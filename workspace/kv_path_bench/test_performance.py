import unittest

from kv_transfer_bench import make_batches, measure_batch, summarize


class PerformanceTest(unittest.TestCase):
    def test_batches_cover_pages_and_l1_without_aliasing(self):
        for count in (1, 9, 137, 1024):
            batches = make_batches(count)
            self.assertEqual([p for b in batches for p in b["pages"]], list(range(count)))
            self.assertEqual(sorted(s for b in batches for s in b["slots"]), list(range(1, count + 1)))
            self.assertEqual(len(batches), 1)
            self.assertEqual(len(batches[0]["pages"]), count)
        self.assertNotEqual(make_batches(1024)[0]["slots"], list(range(1, 9)))
        self.assertEqual(
            [s for b in make_batches(1024, layout="contiguous") for s in b["slots"]],
            list(range(1, 1025)),
        )

    def test_timing_excludes_preparation_and_includes_completion(self):
        now = 0
        calls = []

        def prepare():
            nonlocal now
            now += 1000
            calls.append("prepare")

        def action():
            nonlocal now
            now += 2
            calls.append("transfer")

        def synchronize():
            nonlocal now
            now += 3
            calls.append("sync")

        samples = measure_batch(action, prepare, synchronize, 2, 3, clock=lambda: now)
        self.assertEqual(samples, [5, 5, 5])
        self.assertEqual(calls, ["prepare", "sync", "transfer", "sync"] * 5)

    def test_whole_request_samples_are_not_summed(self):
        result = summarize("C", [10, 5, 6], 12_000_000_000)
        self.assertEqual(result["samples_s"], [10, 5, 6])
        self.assertEqual(result["median_s"], 6)
        self.assertEqual(result["p95_s"], 10)
        self.assertEqual(result["effective_gbps"], 2)
        self.assertEqual(result["measurement_protocol"], "whole_request_v2")
        self.assertFalse(result["validation_enabled"])
        self.assertIsNone(result["correct"])
        self.assertTrue(summarize("C", [1], 1, validate=True)["correct"])

    def test_validation_is_outside_timing_and_runs_each_iteration(self):
        now = [0]
        checked = []
        def action():
            now[0] += 3
        def validate():
            checked.append(True)
            now[0] += 1000
        values = measure_batch(action, lambda: None, lambda: None, 2, 10,
                               clock=lambda: now[0], validate=validate)
        self.assertEqual(values, [3] * 10)
        self.assertEqual(len(checked), 12)


if __name__ == "__main__":
    unittest.main()
