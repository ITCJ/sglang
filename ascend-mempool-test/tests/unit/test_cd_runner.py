"""Admission, paired orchestration, progress and exact-case resume for C/D."""

import concurrent.futures
import json
import socket
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cd_resume
import run_numa_cd as runner
from cd_progress import CDProgress
from cd_results import admit, load_reused, local_groups, save, unit
from ticket10_processes import Peer

from ascend_mempool.cd_plan import CDCase, cases, expected_bytes, topologies

CODE = dict(git_sha="commit", source_sha256="digest")
CPUS = [[0], [1], [2], [3]]


def args(*extra, role="prefill"):
    return runner.parse_args(["--role", role, "--run-id", "new", *extra])


def checkpoint(contract, case, side, verified=True):
    payload, logical = expected_bytes(
        case,
        contract["topology"]["tp_size"],
        contract["topology"]["mode"],
        contract["member"],
    )
    rows = []
    if contract["side"] == side:
        for sample in range(contract["samples"]):
            start = 10**9 * (1 + sample) + contract["member"] * 1000
            rows.append(
                dict(
                    member=contract["member"],
                    side=side,
                    sample=sample,
                    attempt=0,
                    start_ns=start,
                    done_ns=start + 5_000_000,
                    device_ms=4.0,
                    repeats=contract["repeats"],
                    copy_bytes=payload,
                    logical_bytes=logical,
                    opposite_side_idle=True,
                    accepted=True,
                )
            )
    return dict(
        status="checked",
        contract=deepcopy(contract),
        case=case.signature(),
        consumer_side=side,
        samples=rows,
        numa_verified=verified,
        environment=dict(driver="v1", host=contract["side"]),
    )


class TestCDRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = args("--samples", "2")
        self.topology = topologies([0, 2, 4, 6])[2]
        self.contracts = [
            runner.contract(self.args, CODE, self.topology, 0, m, CPUS)
            for m in range(2)
        ]
        self.case = CDCase("write-logical-2048", "logical", rows=2048)
        self.records = [checkpoint(c, self.case, 0) for c in self.contracts]

    def test_cli_modes_keep_graph_48_and_full_matrix_separate(self):
        full = runner.describe(args())
        self.assertEqual(full["scope"], "cd_component")
        self.assertEqual(full["measured_cases_both_sides"], 486)
        self.assertEqual(full["measurement"]["engine"], "graph")
        self.assertEqual(full["measurement"]["block_dim"], 48)
        self.assertEqual(
            runner.describe(args("--read-sources", "local"))[
                "measured_cases_both_sides"
            ],
            318,
        )
        self.assertEqual(
            runner.describe(args("--preflight-only"))["measured_cases_both_sides"], 0
        )
        smoke = runner.describe(args("--smoke"))
        self.assertEqual(smoke["scope"], "cd_smoke")
        self.assertEqual(smoke["measurement"]["topk"], 2048)
        with patch("sys.stderr"):
            for flags in (
                ("--devices", "0", "1", "2", "2"),
                ("--numa-nodes", "0", "0", "4", "6"),
                ("--samples", "0"),
                ("--timeout", "nan"),
                ("--run-id", "../bad"),
                ("--resume-from", "new"),
                ("--control-port", "65535"),
            ):
                with self.subTest(flags=flags), self.assertRaises(SystemExit):
                    args(*flags)

    def test_group_envelope_and_bytes_use_all_concurrent_ranks(self):
        groups = local_groups(self.records)
        self.assertEqual(len(groups), 2)
        self.assertAlmostEqual(groups[0]["group_ms"], 5.001 / 64)
        self.assertEqual(groups[0]["copy_bytes"], 2048 * 1152 * 64)
        summary = runner.summarize(groups)[0]
        self.assertEqual(summary["batches"], 2)
        self.assertEqual(summary["copy_bytes_per_call"], 2048 * 1152)
        self.assertEqual(len(summary["device_per_call_ms"]), 2)
        self.assertEqual(summary["round_p50_ms"], {"0": 5.001 / 64})

    def test_missing_wrong_size_bad_timing_or_misaligned_rank_is_rejected(self):
        with self.assertRaises(ValueError):
            local_groups(self.records[:1])
        mutations = [
            lambda r: r[1]["samples"].pop(),
            lambda r: r[1]["samples"][0].update(copy_bytes=2048 * 1152),
            lambda r: r[1]["samples"][0].update(device_ms=float("nan")),
            lambda r: r[1]["samples"][0].update(start_ns=10**9 + 2_000_000),
            lambda r: r[1]["samples"][0].update(attempt=1),
            lambda r: r[1]["samples"][0].update(opposite_side_idle=False),
        ]
        for mutate in mutations:
            rows = deepcopy(self.records)
            mutate(rows)
            with self.assertRaises(ValueError):
                local_groups(rows)

    def test_resume_rejects_contract_or_environment_changes_and_keeps_pending_evidence(
        self,
    ):
        path = self.root / "case.json"
        record = self.records[0]
        record["numa_verified"] = False
        save(path, record)
        reused = load_reused(
            path, self.contracts[0], self.case, 0, 0, record["environment"]
        )
        self.assertFalse(reused["numa_verified"])
        for key, value in (
            ("device_id", 3),
            ("numa_node", 2),
            ("cpu_affinity", [9]),
            ("code", {}),
            ("repeats", 1),
        ):
            changed = deepcopy(self.contracts[0])
            changed[key] = value
            with self.assertRaises(ValueError):
                admit(record, changed, self.case, 0, 0)
        with self.assertRaisesRegex(ValueError, "environment"):
            load_reused(path, self.contracts[0], self.case, 0, 0, dict(driver="v2"))

    def test_audited_resume_keeps_original_code_and_all_other_checks(self):
        records = deepcopy(self.records)
        old_contracts = [dict(c, code=cd_resume.BASELINE) for c in self.contracts]
        for member, record in enumerate(records):
            record["contract"] = old_contracts[member]
            save(self.root / f"member-{member}/cases/{unit(self.case, 0)}.json", record)
        with patch.object(cd_resume, "_audited_current", return_value=True):
            self.assertEqual(
                runner.resume_units(self.root, self.contracts, self.args, 2),
                [unit(self.case, 0)],
            )
            path = self.root / f"member-0/cases/{unit(self.case, 0)}.json"
            reused = load_reused(
                path, self.contracts[0], self.case, 0, 0, records[0]["environment"]
            )
            self.assertEqual(reused["contract"]["code"], cd_resume.BASELINE)
            self.assertEqual(reused["samples"], records[0]["samples"])
            changed = dict(self.contracts[0], numa_node=2)
            with self.assertRaises(ValueError):
                admit(records[0], changed, self.case, 0, 0)
            with self.assertRaisesRegex(ValueError, "environment"):
                load_reused(
                    path, self.contracts[0], self.case, 0, 0, dict(driver="changed")
                )
        # Valid source identity cannot bypass the destination runtime audit.
        with patch.object(cd_resume, "_audited_current", return_value=False):
            with self.assertRaises(ValueError):
                runner.resume_units(self.root, self.contracts, self.args, 2)

    def test_partial_checkpoint_reuse_requires_every_local_rank_and_valid_idle_records(
        self,
    ):
        for member in range(2):
            save(
                self.root / f"member-{member}/cases/{unit(self.case, 0)}.json",
                self.records[member],
            )
        self.assertEqual(
            runner.resume_units(self.root, self.contracts, self.args, 2),
            [unit(self.case, 0)],
        )
        (self.root / f"member-1/cases/{unit(self.case, 0)}.json").unlink()
        self.assertEqual(
            runner.resume_units(self.root, self.contracts, self.args, 2), []
        )
        idle = [checkpoint(c, self.case, 1) for c in self.contracts]
        self.assertEqual(local_groups(idle), [])
        idle[1]["status"] = "failed"
        with self.assertRaises(ValueError):
            local_groups(idle)

    def test_waiting_for_peer_close_keeps_progress_and_enforces_cleanup_deadline(self):
        config = args(role="decode")
        topology = topologies(config.numa_nodes)[0]
        phase = "round-0/" + topology.name
        progress = CDProgress(self.root, 100)
        peer = Mock()

        def expect(kind, check=None):
            if kind == "reuse":
                return dict(phase=phase, units=[])
            self.assertEqual(kind, "barrier")
            self.assertEqual(progress.snapshot()["current"], "waiting-peer-close")
            self.assertIsNotNone(check)
            # The worker has finished but D never acknowledges closing.
            progress.cleanup_started = runner.time.monotonic() - 121
            check()
            self.fail("cleanup deadline was not enforced while waiting for peer")

        peer.expect.side_effect = expect
        with patch.object(runner, "Processes", FakeProcesses):
            with self.assertRaisesRegex(TimeoutError, "cleanup exceeded 120s"):
                runner.run_group(
                    config,
                    peer,
                    self.root,
                    topology,
                    0,
                    dict(code=CODE),
                    CPUS,
                    progress,
                    None,
                    preflight=False,
                )

    def test_progress_refreshes_fast_reuse_before_waiting_for_peer_close(self):
        with patch("cd_progress.time.monotonic", return_value=100):
            progress = CDProgress(self.root, 10)
            progress.begin("tp4", self.root, 4, 2)
            for member in range(4):
                (self.root / f"member-{member}.log").write_text(
                    "PASS one REUSED\nPASS two REUSED\nCLEANUP closed\n"
                )
            # Force a read even within the 1s polling interval, before blocking
            # on the peer. This used to leave P showing stale setup and counts.
            progress.update(status="waiting-peer-close")
        state = json.loads((self.root / "progress.json").read_text())
        self.assertEqual(state["completed"], 2)
        self.assertEqual(state["current"], "waiting-peer-close")
        self.assertIsNone(state["eta_s"])
        with patch("cd_progress.time.monotonic", return_value=112):
            progress.update()
        state = json.loads((self.root / "progress.json").read_text())
        self.assertEqual(state["cleanup_elapsed_s"], 12)
        progress.update(finished=True)
        self.assertEqual(progress.snapshot()["completed"], 2)

    def test_progress_reports_a_pending_cleanup_member_without_counting_it_as_work(
        self,
    ):
        progress = CDProgress(self.root, 10)
        progress.begin("tp4", self.root, 4, 2)
        for member in range(4):
            phase = "pool-close" if member == 2 else "closed"
            (self.root / f"member-{member}.log").write_text(
                f"PASS one REUSED\nPASS two REUSED\nCLEANUP {phase}\n"
            )
        progress.last_poll = progress.last_print = 0
        progress.update()
        self.assertEqual(progress.snapshot()["current"], "cleanup/pool-close")
        self.assertEqual(progress.snapshot()["completed"], 2)
        self.assertIsNotNone(progress.cleanup_started)

    def test_progress_tracks_slowest_rank_then_accepts_a_different_width(self):
        progress = CDProgress(self.root, 6)
        progress.begin("tp4", self.root, 4, 4)
        for m in range(4):
            (self.root / f"member-{m}.log").write_text(
                "BEGIN one\nPASS one\n" + ("PASS two\n" if m < 3 else "")
            )
        progress.last_poll = progress.last_print = 0
        progress.update()
        self.assertEqual(progress.snapshot()["completed"], 1)
        progress.update(finished=True)
        self.assertEqual(progress.snapshot()["completed"], 4)
        progress.begin("tp1", self.root / "next", 1, 2)
        progress.update(finished=True)
        self.assertEqual(progress.snapshot()["completed"], 6)
        self.assertEqual(progress.snapshot()["eta_s"], 0)


class FakeProcesses:
    """Only the subprocess/NPU boundary is simulated; paired orchestration is real."""

    def __init__(self, root):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def start(self, name, command):
        config = json.loads(Path(command[command.index("--config") + 1]).read_text())
        output = Path(command[command.index("--output") + 1])
        c = config["cd_contract"]
        n = c["topology"]["tp_size"]
        log = []
        if not config["cd_preflight"]:
            for case in cases(
                n,
                c["measurement"]["sources"],
                smoke=config["smoke"],
                contiguous=c["measurement"]["contiguous"],
            ):
                for side in (0, 1):
                    save(
                        output / "cases" / f"{unit(case, side)}.json",
                        checkpoint(c, case, side, verified=False),
                    )
                    log.append("PASS " + unit(case, side))
        save(
            output / "report.json",
            dict(
                status="checks_passed_numa_unverified",
                closed=True,
                config=config,
                mapping=dict(
                    member=c["member"],
                    local_dram_bytes=2 << 30,
                    numa=dict(placement_verified=False),
                ),
                cases_numa_verified=False,
            ),
        )
        (self.root / f"{name}.log").write_text("\n".join(log) + "\n")
        return SimpleNamespace(poll=lambda: 0)

    def wait(self, name, child, *, timeout, check):
        check()


class TestPairedCDOrchestration(unittest.TestCase):
    def test_all_preflights_precede_rotated_groups_and_both_summaries_retain_pending(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            sockets = socket.socketpair()
            for connection in sockets:
                connection.settimeout(30)
            original = Path.read_text

            def read(path, *args, **kwargs):
                if str(path) == "/sys/devices/system/node/online":
                    return "0,2,4,6"
                if str(path) == "/sys/devices/system/node/node0/cpulist":
                    return "0-3"
                return original(path, *args, **kwargs)

            def execute(side):
                role = ("prefill", "decode")[side]
                config = args(
                    "--rounds", "1", "--samples", "1", "--log-root", temp, role=role
                )
                root = Path(temp) / "new" / role
                root.mkdir(parents=True)
                with Peer(sockets[side], 30) as peer:
                    return runner.execute(config, peer, root)

            with (
                patch.object(Path, "read_text", read),
                patch.object(
                    runner.os,
                    "sched_getaffinity",
                    return_value={0, 1, 2, 3},
                    create=True,
                ),
                patch.object(runner, "code_identity", return_value=CODE),
                patch.object(runner, "CohortServer"),
                patch.object(runner, "Processes", FakeProcesses),
                patch("builtins.print"),
                concurrent.futures.ThreadPoolExecutor(2) as threads,
            ):
                results = list(threads.map(execute, (0, 1)))
            for result in results:
                self.assertEqual(result["status"], "component_results_pending_numa")
                self.assertEqual(result["exit_code"], 2)
                self.assertEqual(len(result["comparison"]), 81)
                preflights = [r["preflight"] for r in result["physical_allocations"]]
                first_measured = preflights.index(False)
                self.assertTrue(all(preflights[:first_measured]))
                self.assertFalse(any(preflights[first_measured:]))
                self.assertEqual(
                    len(
                        {
                            r["topology"]
                            for r in result["physical_allocations"][:first_measured]
                        }
                    ),
                    7,
                )
            root = Path(temp) / "new/prefill"
            self.assertTrue((root / "decode-summary.json").exists())
            self.assertEqual(
                len((root / "combined-results.csv").read_text().splitlines()), 163
            )
            progress = json.loads((root / "progress.json").read_text())
            self.assertEqual(progress["completed"], progress["total"])


if __name__ == "__main__":
    unittest.main()
