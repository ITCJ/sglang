import unittest

from kv_transfer_bench import make_batches, measure_batch, summarize


class PerformanceTest(unittest.TestCase):
    def test_batches_cover_pages_and_l1_without_aliasing(self):
        for count in (1, 9, 137, 1024):
            batches = make_batches(count)
            self.assertEqual([p for b in batches for p in b["pages"]], list(range(count)))
            self.assertEqual(sorted(s for b in batches for s in b["slots"]), list(range(1, count + 1)))
            self.assertTrue(all(1 <= len(b["pages"]) <= 8 for b in batches))
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

    def test_workload_samples_sum_batches_before_statistics(self):
        result = summarize("B", [[1, 3, 2], [9, 2, 4]], 12_000_000_000, 128)
        self.assertEqual(result["samples_s"], [10, 5, 6])
        self.assertEqual(result["median_s"], 6)
        self.assertEqual(result["p95_s"], 10)
        self.assertEqual(result["effective_gbps"], 2)
        self.assertEqual(result["host_staging_bytes"], 128)


if __name__ == "__main__":
    unittest.main()
