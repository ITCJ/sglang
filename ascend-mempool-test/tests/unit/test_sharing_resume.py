"""Resume never upgrades failed groups or silently combines incompatible runs."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_numa_sharing as runner
from sharing_progress import Progress
from sharing_resume import (
    COMPATIBLE_SOURCE,
    check_resume_environment,
    load_completed_rounds,
)


class TestResume(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = runner.parse_args(
            [
                "--role",
                "prefill",
                "--prefill-host",
                "p",
                "--decode-host",
                "d",
                "--run-id",
                "new",
                "--numa-node",
                "0",
                "--resume-from",
                "old",
                "--resume-round",
                "2",
                "--log-root",
                str(self.root),
            ]
        )
        self.plan = runner.describe(self.args)
        self.source = self.root / "old/prefill"
        self.source.mkdir(parents=True)
        self.write(
            self.source / "identity.json",
            dict(run_id="old", plan=self.plan, code=COMPATIBLE_SOURCE),
        )
        self.paths = []
        for rid, order in enumerate(self.plan["mode_order"][:2]):
            for mode in order:
                for member in (0, 1):
                    config = dict(
                        plan=self.plan["plan"],
                        mode=mode,
                        round=rid,
                        member=member,
                        side=0,
                        device_id=member,
                        numa_node=0,
                        prefill_host="p",
                        decode_host="d",
                        topk=2048,
                        block_dims=[24, 48],
                        samples=3,
                        repeats=64,
                        warmup=3,
                        delay_matmuls=512,
                        smoke=False,
                        diagnostic=False,
                        identity=f"old/round-{rid}/{mode}",
                    )
                    rows = [
                        dict(
                            engine=e,
                            variant=v,
                            publication_readback_passed=True,
                            selected_before_writer_done=True,
                            writer_minus_selected_ms=2,
                            selected_ms=1,
                            writer_ms=3,
                        )
                        for e in ("eager", "graph")
                        for v in (0, 1)
                    ]
                    path = (
                        self.source / f"round-{rid}/{mode}/member-{member}/report.json"
                    )
                    self.write(
                        path,
                        dict(
                            status="checks_passed_numa_unverified",
                            closed=True,
                            environment={
                                k: "test"
                                for k in (
                                    "target_memfabric",
                                    "memfabric",
                                    "torch",
                                    "torch_npu",
                                    "sgl_kernel_npu",
                                    "unidex_schema",
                                    "device",
                                    "hostname",
                                    "kernel",
                                    "cpu_affinity",
                                    "/usr/local/Ascend/driver/version.info",
                                    "/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
                                )
                            },
                            config=config,
                            samples=[],
                            delayed_publication=rows,
                        ),
                    )
                    for engine in ("eager", "graph"):
                        self.write(
                            path.parent / f"delayed-{engine}-trace.json",
                            {"traceEvents": []},
                        )
                    self.paths.append(path)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def load(self):
        return load_completed_rounds(
            self.args, self.plan, dict(git_sha="new", source_sha256="new")
        )

    def test_only_completed_first_two_rounds_are_loaded(self):
        reports, provenance = self.load()
        self.assertEqual(
            list(reports),
            [
                "round-0/independent",
                "round-0/shared",
                "round-1/shared",
                "round-1/independent",
            ],
        )
        self.assertEqual(provenance["source_identity"]["code"], COMPATIBLE_SOURCE)
        self.assertEqual(
            reports["round-0/independent"][0]["status"], "checks_passed_numa_unverified"
        )
        # Missing performance samples still fail the existing aggregator gate.
        with self.assertRaises(RuntimeError):
            runner.local_groups(reports["round-0/independent"])

    def test_failed_unclosed_wrong_device_and_missing_publication_are_rejected(self):
        path = self.paths[-1]
        original = json.loads(path.read_text())
        for field in ("status", "closed", "device", "publication", "overlap"):
            data = deepcopy(original)
            if field == "status":
                data["status"] = "failed"
            elif field == "closed":
                data["closed"] = False
            elif field == "device":
                data["config"]["device_id"] = 3
            elif field == "publication":
                data["delayed_publication"].pop()
            else:
                data["delayed_publication"][0]["writer_minus_selected_ms"] = -0.23
            self.write(path, data)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load()
        self.write(path, original)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()

    def test_unknown_code_or_changed_plan_is_not_reused(self):
        path = self.source / "identity.json"
        original = json.loads(path.read_text())
        for field in ("code", "plan"):
            data = deepcopy(original)
            data[field] = {}
            self.write(path, data)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load()

    def test_changed_destination_implementation_cannot_claim_legacy_compatibility(self):
        with patch("sharing_resume.implementation_fingerprint", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "audited resume target"):
                self.load()

    def test_environment_change_is_rejected_before_mixing_runs(self):
        original = {"torch_npu": "a", "cpu_affinity": [0, 1]}
        check_resume_environment(original, original)
        with self.assertRaisesRegex(ValueError, "torch_npu"):
            check_resume_environment(dict(original, torch_npu="b"), original)
        path = self.paths[-1]
        report = json.loads(path.read_text())
        report["environment"]["cpu_affinity"] = [9]
        self.write(path, report)
        with self.assertRaisesRegex(ValueError, "cpu_affinity"):
            self.load()

    def test_execute_reuses_four_groups_and_launches_only_third_round(self):
        starts = []
        template = json.loads(self.paths[0].read_text())
        mapping = dict(local_dram_bytes=1, numa=dict(placement_verified=False))
        for path in self.paths:
            data = json.loads(path.read_text())
            data["mapping"] = mapping
            self.write(path, data)
        root = self.root / "new/prefill"
        root.mkdir(parents=True)
        peer = Mock()
        messages = {}
        peer.send.side_effect = lambda kind, **payload: messages.__setitem__(
            kind, payload
        )
        peer.expect.side_effect = lambda kind: messages[kind]

        def start(name, command):
            config = json.loads(
                Path(command[command.index("--config") + 1]).read_text()
            )
            starts.append((config["round"], config["mode"], config["member"]))
            output = Path(command[command.index("--output") + 1])
            report = deepcopy(template)
            report.update(config=config, mapping=mapping)
            self.write(output / "report.json", report)
            return SimpleNamespace(poll=lambda: 0)

        processes = Mock()
        processes.__enter__ = Mock(return_value=processes)
        processes.__exit__ = Mock(return_value=False)
        processes.start.side_effect = start

        def groups(reports):
            config = reports[0]["config"]
            return [dict(round=config["round"], mode=config["mode"])]

        with (
            patch.object(runner, "Processes", return_value=processes),
            patch.object(runner, "CohortServer"),
            patch.object(
                runner,
                "code_identity",
                return_value=dict(git_sha="new", source_sha256="new"),
            ),
            patch.object(runner, "local_groups", side_effect=groups),
            patch.object(runner, "summarize", side_effect=lambda rows: rows),
            patch.object(runner, "receive_log"),
            redirect_stdout(io.StringIO()),
        ):
            result = runner.execute(self.args, peer, root)
        self.assertEqual(
            starts,
            [
                (2, "independent", 0),
                (2, "independent", 1),
                (2, "shared", 0),
                (2, "shared", 1),
            ],
        )
        self.assertEqual(len(result["comparison"]), 6)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(len(result["resumed_from"]["reused_phases"]), 4)
        self.assertTrue((self.source / "identity.json").exists())

    def test_progress_tracks_slower_worker_and_reused_groups_without_fake_completion(
        self,
    ):
        with (
            redirect_stdout(io.StringIO()) as output,
            patch("sharing_progress.time.monotonic", return_value=10),
        ):
            progress = Progress(6, 4)
            progress.begin("round-0/independent", self.root)
            progress.update(finished=True, reused=True)
            progress.begin("round-2/independent", self.root)
            (self.root / "member-0.log").write_text(
                "BEGIN case/a\nPASS case/a\nPASS case/b\n"
            )
            (self.root / "member-1.log").write_text(
                "BEGIN case/a\nPASS case/a\nBEGIN case/b"
            )
            progress.last_print = float("-inf")
            progress.update()
            self.assertEqual(progress.counts, [2, 1])
            with (self.root / "member-1.log").open("a") as stream:
                stream.write("\nPASS case/b\n")
            progress.last_print = float("-inf")
            progress.update()
            self.assertEqual(progress.counts, [2, 2])
        self.assertIn("REUSED", output.getvalue())
        self.assertIn("cases 1/4", output.getvalue())
        self.assertNotIn("100.0%", output.getvalue())


if __name__ == "__main__":
    unittest.main()
