"""Runner admission and reported timing are part of the experiment contract."""

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_numa_sharing as runner


class TestSharingRunner(unittest.TestCase):
    def args(self, *extra):
        return runner.parse_args(
            [
                "--role",
                "prefill",
                "--prefill-host",
                "p",
                "--decode-host",
                "d",
                "--run-id",
                "r1",
                "--numa-node",
                "0",
                *extra,
            ]
        )

    def test_full_plan_keeps_real_2048_topk_and_alternates_pool_order(self):
        plan = runner.describe(self.args("--describe"))
        self.assertEqual(
            plan["mode_order"],
            [
                ["independent", "shared"],
                ["shared", "independent"],
                ["independent", "shared"],
            ],
        )
        self.assertEqual(plan["topk"], 2048)
        self.assertEqual(plan["scope"], "full_component")
        self.assertEqual(plan["shared_contributions"], [1 << 30, 0, 1 << 30, 0])

    def test_bad_configuration_fails_before_starting_children(self):
        for extra in (
            ("--devices", "0", "0"),
            ("--rounds", "0"),
            ("--numa-node", "127"),
            ("--prompt-tokens", "2048"),
            ("--control-port", "65535"),
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.args(*extra)

    def test_smoke_does_not_claim_graph_or_full_acceptance(self):
        plan = runner.describe(self.args("--smoke", "--describe"))
        self.assertEqual(plan["scope"], "eager_smoke")
        self.assertEqual(plan["engines"], ["eager"])

    def test_hang_diagnostic_preserves_preceding_graph_but_cannot_claim_acceptance(
        self,
    ):
        args = self.args("--diagnose-write-read", "--block-dims", "48")
        plan = runner.describe(args)
        self.assertEqual(plan["scope"], "write_read_diagnostic")
        self.assertEqual(plan["mode_order"], [["independent"]])
        self.assertEqual(plan["engines"], ["eager", "graph"])
        self.assertEqual(
            [c["name"] for c in plan["cases"]],
            ["write_then_read-1", "write_then_read-16"],
        )
        self.assertEqual(plan["samples"], 3)
        self.assertEqual(plan["repeats"], 64)
        for verified in (True, False):
            outcome = runner.completion_outcome(
                False, verified, verified, diagnostic=True
            )
            self.assertEqual(outcome["status"], "diagnostic_passed")
            self.assertEqual(outcome["exit_code"], 0)
        with self.assertRaises(SystemExit):
            self.args("--diagnose-write-read", "--smoke")

    def test_unobservable_numa_on_either_host_cannot_claim_full_pass(self):
        for local, peer in ((False, True), (True, False), (False, False)):
            with self.subTest(local=local, peer=peer):
                outcome = runner.completion_outcome(False, local, peer)
                self.assertEqual(outcome["status"], "component_results_pending_numa")
                self.assertEqual(outcome["exit_code"], 2)
                self.assertFalse(outcome["numa_placement_verified"])
        complete = runner.completion_outcome(False, True, True)
        self.assertEqual(complete["status"], "component_checks_passed")
        self.assertEqual(complete["exit_code"], 0)
        smoke = runner.completion_outcome(True, False, False)
        self.assertEqual(smoke["status"], "smoke_passed")
        self.assertEqual(smoke["exit_code"], 0)
        self.assertFalse(smoke["numa_placement_verified"])

    def test_partial_duplicate_or_competing_reports_cannot_pass(self):
        reports = []
        for member in (0, 1):
            config = dict(
                member=member,
                side=1,
                mode="shared",
                round=0,
                smoke=True,
                block_dims=[24],
                samples=1,
                repeats=2,
            )
            rows = [
                dict(
                    case=case.signature(),
                    engine="eager",
                    block_dim=24,
                    sample=0,
                    member=member,
                    side=1,
                    mode="shared",
                    round=0,
                    repeats=2,
                    consumer_side=1,
                    opposite_side_idle=True,
                    start_ns=1000000,
                    done_ns=5000000,
                    device_ms=3.8,
                    device_calls_ms=[1.9, 1.9],
                    selected_calls_ms=[1.8, 1.8],
                    copy_bytes=3456,
                    logical_bytes=3456,
                )
                for case in runner.cases(True)
            ]
            reports.append(
                dict(status="smoke_passed", closed=True, config=config, samples=rows)
            )
        self.assertEqual(len(runner.local_groups(list(reversed(reports)))), 2)
        for mutation in ("missing", "duplicate", "competing", "timing"):
            changed = deepcopy(reports)
            with self.subTest(mutation=mutation):
                if mutation == "missing":
                    for report in changed:
                        report["samples"].pop()
                elif mutation == "duplicate":
                    changed[0]["samples"].append(changed[0]["samples"][0])
                elif mutation == "competing":
                    changed[0]["samples"][0]["opposite_side_idle"] = False
                else:
                    changed[0]["samples"][0]["selected_calls_ms"] = [float("nan")]
                with self.assertRaises(RuntimeError):
                    runner.local_groups(changed)

    def test_reader_efficiency_does_not_present_sum_of_singles_as_dual_bandwidth(self):
        rows = []
        for name, accessor, throughput in (
            ("dual", -1, 120),
            ("owner", 0, 90),
            ("follower", 1, 110),
        ):
            case = runner.cases(True)[1].signature()
            case.update(name=name, accessor=accessor)
            rows.append(
                dict(
                    case=case,
                    side=1,
                    engine="graph",
                    block_dim=24,
                    independent=dict(bm_payload_gbps_p50=throughput),
                    shared=dict(bm_payload_gbps_p50=throughput),
                )
            )
        runner.add_reader_efficiency(rows)
        result = rows[0]["shared"]["dual_reader_efficiency"]
        self.assertEqual(result["fraction"], 0.6)
        self.assertEqual(result["dual_measured_bm_gbps"], 120)
        self.assertEqual(result["single_measured_bm_gbps"], [90, 110])


if __name__ == "__main__":
    unittest.main()
