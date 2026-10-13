"""Conservation, fairness and bounded workload of the redesigned C/D matrix."""

import concurrent.futures
import unittest

from ascend_mempool.cd_plan import (
    CDCase,
    cases,
    cpu_partitions,
    expected_bytes,
    indices,
    schedule,
    topologies,
    write_span,
)
from ascend_mempool.sharing import port_plan
from ascend_mempool.sharing_control import Cohort, CohortServer


class TestCDPlan(unittest.TestCase):
    def test_capacity_and_cpu_placement_do_not_grow_or_move_with_tp(self):
        layouts = topologies([0, 2, 4, 6])
        self.assertEqual(len(layouts), 7)
        self.assertEqual(
            [t.nodes for t in layouts],
            [(0,), (0, 0), (0, 0), (0, 2), (0,) * 4, (0,) * 4, (0, 2, 4, 6)],
        )
        for t in layouts:
            self.assertEqual(
                t.plan().layout().layout_for_rank(0).total_bytes, 1152 * 65536 * 16
            )
            nonzero = [b for b in t.plan().contributions(t.mode) if b]
            self.assertEqual(set(nonzero), {2 << 30})
            self.assertEqual(len(nonzero), 2 if t.mode == "shared" else 2 * t.tp_size)
        partitions = cpu_partitions(set(range(3, 23)))
        self.assertEqual(set.union(*(set(p) for p in partitions)), set(range(3, 23)))
        self.assertEqual(sum(map(len, partitions)), 20)
        self.assertEqual(partitions[0], [3, 7, 11, 15, 19])

    def test_three_rounds_balance_width_and_layout_order(self):
        order = schedule([0, 2, 4, 6], 3)
        self.assertEqual(len(order), 21)
        for n in (2, 4):
            for position in range(3):
                names = [
                    [t.name for r, t in order if r == i and t.tp_size == n][position]
                    for i in range(3)
                ]
                self.assertEqual(len(set(names)), 3)
        self.assertEqual(
            [next(t.tp_size for r, t in order if r == i) for i in range(3)], [1, 2, 4]
        )

    def test_exact_case_count_and_fixed_topk_load(self):
        layouts = topologies([0, 2, 4, 6])
        self.assertEqual(
            sum(len(cases(t.tp_size, ["local", "remote", "mixed"])) for t in layouts),
            81,
        )
        self.assertEqual(sum(len(cases(t.tp_size, ["local"])) for t in layouts), 53)
        read = CDCase("read", "read", active=16)
        for n in (1, 2, 4):
            self.assertEqual(expected_bytes(read, n, "shared", 0)[0], 36 * 1024**2)

    def test_shared_and_control_partitions_are_complete_disjoint_and_balanced(self):
        for n in (1, 2, 4):
            for rows in (2048, 2049, 8192, 32768):
                for mode, kind in (
                    ("shared", "logical"),
                    ("shared", "equal_total"),
                    ("independent", "equal_total"),
                ):
                    spans = [write_span(rows, m, n, mode, kind) for m in range(n)]
                    self.assertEqual(spans[0][0], 0)
                    self.assertEqual(spans[-1][1], rows)
                    self.assertTrue(all(a[1] == b[0] for a, b in zip(spans, spans[1:])))
                    counts = [b - a for a, b in spans]
                    self.assertLessEqual(max(counts) - min(counts), 1)
                    case = CDCase("write", kind, rows=rows)
                    self.assertEqual(
                        sum(expected_bytes(case, n, mode, m)[0] for m in range(n)),
                        rows * 1152,
                    )
                for m in range(n):
                    self.assertEqual(
                        write_span(rows, m, n, "independent", "logical"), (0, rows)
                    )

    def test_identical_sparse_indices_across_ranks_change_between_batches(self):
        case = CDCase("read", "read", active=16)
        first = indices(case, 0, 41)
        self.assertEqual(first, indices(case, 0, 41))
        self.assertNotEqual(first, indices(case, 1, 41))
        for selection in first:
            self.assertEqual(len(set(selection)), 2048)
            self.assertGreater(max(selection), 32768)
            self.assertLess(max(selection), 65536)
        self.assertNotEqual(first[0], first[1])

    def test_real_tp1_pair_maps_and_drains_without_dummy_ranks(self):
        server = CohortServer("127.0.0.1", 0, "tp1", 3, 1)
        self.addCleanup(server.close)
        port = server.server.server_address[1]

        def worker(rank):
            cohort = Cohort("127.0.0.1", port, rank, "tp1", 3, 1)
            try:
                mapped = cohort.exchange("mapped", side=rank)
                cohort.exchange("drained", success=True, drained=True)
                return mapped
            finally:
                cohort.close()

        with concurrent.futures.ThreadPoolExecutor(2) as threads:
            result = list(threads.map(worker, range(2)))
        self.assertEqual(result, [[dict(side=0), dict(side=1)]] * 2)
        self.assertEqual(port_plan(19400, "independent", 1, 0, 1)["store"], 19402)


if __name__ == "__main__":
    unittest.main()
