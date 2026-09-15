import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import check  # Adds the sibling benchmark helpers to the import path.
import performance as perf
from kv_transfer_bench import make_batches


class FabricPathsTest(unittest.TestCase):
    def test_l2_scatter_composes_to_identical_direct_transfer(self):
        for layout in ("contiguous", "scattered"):
            for batch in make_batches(32, 8, layout):
                plans = perf.batch_plans(batch, 1 << 40, 2 << 40, 3 << 40, 4 << 40, 32)
                read_src, read_dst, read_sizes = plans["read"]
                local_src, targets, sizes = plans["local"]
                direct_src, direct_targets, direct_sizes = plans["direct"]
                self.assertEqual(targets, direct_targets)
                self.assertEqual(sizes, direct_sizes)
                self.assertEqual(sum(read_sizes), len(batch["pages"]) * check.PAGE_BYTES)
                for local, direct, length in zip(local_src, direct_src, sizes):
                    matches = [(src + local - dst) for src, dst, size in
                               zip(read_src, read_dst, read_sizes)
                               if dst <= local and local + length <= dst + size]
                    self.assertEqual(matches, [direct])
                for dst, size in zip(read_dst, read_sizes):
                    self.assertGreater(dst, 2 << 40)
                    self.assertLessEqual(dst + size, (2 << 40) + perf.L2_BYTES)

    def test_timed_paths_have_only_required_transfers_and_waits(self):
        bm = SimpleNamespace(BmCopyType=SimpleNamespace(G2G="host-host", GH2L="host-npu"))
        plans = {name: ([ptr], [ptr + 1], [8]) for ptr, name in
                 enumerate(("read", "local", "direct"), 10)}
        for code, names in (("E", ["local"]), ("F", ["read", "local"]), ("D", ["direct"]), ("G", ["read"])):
            handle = Mock()
            handle.copy_data_batch.return_value = handle.wait.return_value = 0
            perf.run_path(code, handle, bm, plans)
            expected = []
            from unittest.mock import call
            for name in names:
                expected.extend([call.copy_data_batch(*plans[name], 1,
                                "host-host" if name == "read" else "host-npu", 0), call.wait()])
            self.assertEqual(handle.mock_calls, expected)

    def test_failed_first_leg_never_runs_second(self):
        bm = SimpleNamespace(BmCopyType=SimpleNamespace(G2G=1, GH2L=2))
        handle = Mock()
        handle.copy_data_batch.return_value = -1
        with self.assertRaises(RuntimeError):
            perf.run_path("F", handle, bm, {"read": ([1], [2], [8])})
        handle.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
