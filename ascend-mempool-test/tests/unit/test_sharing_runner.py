"""Runner admission and reported timing are part of the experiment contract."""

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_numa_sharing as runner


class TestSharingRunner(unittest.TestCase):
    tp_size = 2

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

    def test_tp4_plan_and_capacity_are_explicit_before_starting_children(self):
        plan = runner.describe(self.args("--devices", "0", "1", "2", "3"))
        self.assertEqual(plan["plan"]["tp_size"], 4)
        self.assertEqual(plan["plan"]["layers"], 4)
        self.assertEqual(plan["plan"]["prompt_tokens"], 16384)
        self.assertEqual(plan["shared_contributions"], [2 << 30, 0, 0, 0] * 2)
        self.assertEqual(plan["independent_contributions"], [2 << 30] * 8)
        self.assertEqual(len(plan["cases"]), 53)
        self.assertEqual({c["accessor"] for c in plan["cases"]}, {-1, 0, 1, 2, 3})
        self.assertEqual({c["layer"] for c in plan["cases"]}, {0, 1, 2, 3})
        for extra in (
            ("--devices", "0", "1", "2"),
            ("--devices", "0", "1", "2", "2"),
            ("--devices", "0", "1", "2", "3", "--prompt-tokens", "8192"),
            ("--devices", "0", "1", "2", "3", "--decode-tokens", "8192"),
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.args(*extra)

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

    def test_independent_diagnostic_preserves_entire_original_matrix(self):
        full = runner.describe(self.args())
        args = self.args("--diagnose-independent")
        diagnostic = runner.describe(args)
        self.assertTrue(args.diagnostic)
        self.assertEqual(diagnostic["scope"], "independent_diagnostic")
        self.assertEqual(diagnostic["mode_order"], [["independent"]])
        for field in (
            "cases",
            "block_dims",
            "engines",
            "consumer_sides",
            "samples",
            "repeats",
            "warmup",
            "plan",
            "topk",
        ):
            self.assertEqual(diagnostic[field], full[field], field)
        self.assertEqual(diagnostic["block_dims"], [24, 48])
        for extra in ("--smoke", "--diagnose-write-read"):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.args("--diagnose-independent", extra)

    def test_publication_diagnostic_skips_benchmark_but_requires_publication_rows(self):
        args = self.args("--diagnose-publication")
        plan = runner.describe(args)
        self.assertEqual(plan["scope"], "publication_diagnostic")
        self.assertEqual(plan["mode_order"], [["independent", "shared"]])
        self.assertEqual(plan["cases"], [])
        reports = []
        for member in range(self.tp_size):
            reports.append(
                dict(
                    status="checks_passed",
                    closed=True,
                    config=dict(
                        plan=dict(tp_size=self.tp_size),
                        member=member,
                        side=1,
                        mode="shared",
                        round=0,
                        smoke=False,
                        block_dims=[24, 48],
                        samples=3,
                        repeats=64,
                        diagnostic=True,
                        diagnose_publication=True,
                    ),
                    samples=[],
                    delayed_publication=[
                        dict(
                            engine=engine,
                            variant=variant,
                            publication_readback_passed=True,
                            selected_before_writer_done=True
                            if engine == "eager"
                            else None,
                            selected_ms=2.0,
                            writer_ms=5.0,
                            writer_minus_selected_ms=3.0,
                        )
                        for engine in ("eager", "graph")
                        for variant in (0, 1)
                    ],
                )
            )
        self.assertEqual(runner.local_groups(reports), [])
        for mutation in ("missing", "duplicate", "readback", "overlap"):
            changed = deepcopy(reports)
            rows = changed[1]["delayed_publication"]
            if mutation == "missing":
                rows.pop()
            elif mutation == "duplicate":
                rows[-1] = rows[0]
            elif mutation == "readback":
                rows[0]["publication_readback_passed"] = False
            else:
                rows[0]["writer_minus_selected_ms"] = -1
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                runner.local_groups(changed)

    def test_partial_duplicate_or_competing_reports_cannot_pass(self):
        reports = []
        for member in range(self.tp_size):
            config = dict(
                plan=dict(tp_size=self.tp_size),
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
        groups = runner.local_groups(list(reversed(reports)))
        self.assertEqual(len(groups), 2)
        independent = deepcopy(groups)
        for group in independent:
            group["mode"] = "independent"
        comparison = runner.summarize(groups + independent)
        for row in comparison:
            self.assertEqual(row["tp_size"], self.tp_size)
            self.assertEqual(len(row["shared"]["per_rank_device_ms"]), self.tp_size)
            self.assertEqual(len(row["shared"]["per_rank_selected_ms"]), self.tp_size)
            self.assertEqual(row["shared_over_independent_group_p50"], 1.0)
        for mutation in (
            "missing",
            "duplicate",
            "competing",
            "timing",
            "member",
            "width",
        ):
            changed = deepcopy(reports)
            with self.subTest(mutation=mutation):
                if mutation == "missing":
                    for report in changed:
                        report["samples"].pop()
                elif mutation == "duplicate":
                    changed[0]["samples"].append(changed[0]["samples"][0])
                elif mutation == "competing":
                    changed[0]["samples"][0]["opposite_side_idle"] = False
                elif mutation == "member":
                    changed.pop()
                elif mutation == "width":
                    changed[-1]["config"]["plan"]["tp_size"] = (
                        2 if self.tp_size == 4 else 4
                    )
                else:
                    changed[0]["samples"][0]["selected_calls_ms"] = [float("nan")]
                with self.assertRaises(RuntimeError):
                    runner.local_groups(changed)

    def test_four_reader_efficiency_uses_all_four_single_measurements(self):
        rows = []
        for accessor, throughput in ((-1, 300), (0, 90), (1, 110), (2, 100), (3, 200)):
            case = runner.cases(True)[1].signature()
            case.update(name=str(accessor), accessor=accessor)
            rows.append(
                dict(
                    case=case,
                    side=1,
                    engine="graph",
                    block_dim=24,
                    tp_size=4,
                    independent=dict(bm_payload_gbps_p50=throughput),
                    shared=dict(bm_payload_gbps_p50=throughput),
                )
            )
        runner.add_reader_efficiency(rows[:-1])
        self.assertNotIn("reader_efficiency", rows[0]["shared"])
        runner.add_reader_efficiency(rows)
        result = rows[0]["shared"]["reader_efficiency"]
        self.assertEqual(result["fraction"], 0.6)
        self.assertEqual(result["concurrent_measured_bm_gbps"], 300)
        self.assertEqual(result["single_measured_bm_gbps"], [90, 110, 100, 200])
        self.assertNotIn("dual_reader_efficiency", rows[0]["shared"])

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


class TestTP4Runner(TestSharingRunner):
    tp_size = 4


if __name__ == "__main__":
    unittest.main()
