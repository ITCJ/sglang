"""Ticket14 public plan/report contracts; no NPU dependency."""

import unittest

from ascend_mempool.bandwidth import BenchmarkConfig, build_plan, write_pages


class TestBandwidthPlan(unittest.TestCase):
    def test_eight_k_is_one_layer_and_writers_partition_one_copy(self):
        config = BenchmarkConfig()
        plan = build_plan(config)
        case = next(c for c in plan if c.name == "write-8192-dual")
        self.assertEqual(case.logical_bytes(config), 9437184)
        pages = [write_pages(config, case, rank) for rank in range(16)]
        self.assertEqual([len(p) for p in pages], [8] * 16)
        self.assertEqual(sorted(p for group in pages for p in group), list(range(128)))
        self.assertEqual(len(plan), len({c.name for c in plan}))
        self.assertEqual(
            {c.tokens for c in plan if c.group == "remote_write"},
            {4096, 8192, 32768, 131072},
        )

    def test_report_uses_concurrent_span_and_actual_operation_samples(self):
        from ascend_mempool.bandwidth_report import summarize

        rows = [
            dict(
                case="read",
                rank=r,
                sample=s,
                start_ns=r * 1000000,
                done_ns=(r + 10) * 1000000,
                bytes_per_op=1000000,
                segments={"P/0": 1000000},
                durations_ms=[1.0, 9.0],
                cpu_seconds=0.001,
            )
            for s in range(3)
            for r in range(2)
        ]
        result = summarize(rows)["read"]
        self.assertAlmostEqual(result["median_GBps"], 4 / 11)
        self.assertEqual(result["operation_samples"], 12)
        self.assertEqual(result["p99_ms"], 9.0)
        self.assertEqual(result["per_rank"]["0"]["median_ms"], 5.0)
        self.assertAlmostEqual(result["segments_GBps"]["P/0"], 4 / 11)
        self.assertAlmostEqual(result["min_overlap_fraction"], 9 / 11)

    def test_empty_or_incomplete_measurements_cannot_be_reported_as_passed(self):
        from ascend_mempool.bandwidth_report import validate_measurements

        with self.assertRaisesRegex(ValueError, "missing|coverage"):
            validate_measurements(BenchmarkConfig(suite="remote-write"), [], [])

    def test_layer_completion_waits_for_all_writers_before_taking_median(self):
        from ascend_mempool.bandwidth_report import summarize

        rows = [
            dict(
                case="dual",
                rank=r,
                sample=s,
                start_ns=0,
                done_ns=10000000,
                first_operation_start_ns=0,
                first_operation_done_ns=9000000 if r == s else 1000000,
                bytes_per_op=100,
                segments={"D/0": 100},
                durations_ms=[9.0 if r == s else 1.0],
                cpu_seconds=0.001,
            )
            for s in range(3)
            for r in range(3)
        ]
        result = summarize(rows)["dual"]
        self.assertEqual(result["first_group_completion_median_ms"], 9.0)
        self.assertEqual(
            [v["median_ms"] for v in result["per_rank"].values()], [1.0] * 3
        )


if __name__ == "__main__":
    unittest.main()
