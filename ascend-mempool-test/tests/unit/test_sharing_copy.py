"""Content checks through UniDexCopy's injected device/SDK boundary."""

import unittest

import torch
from test_pool import FakeBM

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.sharing_cases import Case
from ascend_mempool.sharing_copy import CopyWorkload
from ascend_mempool.verification import kv_pattern


class TestSharedCopies(unittest.TestCase):
    tp_size = 2

    def setUp(self):
        self.layout = PoolLayout(
            KVLayout(self.tp_size, self.tp_size, 64, 1, 576),
            KVLayout(self.tp_size, self.tp_size, 128, 1, 576),
        )
        self.pools = []
        self.memory = {}
        for member in range(self.tp_size):
            pool = MempoolKVManager.create(self.layout, 1, bm_module=FakeBM(1))
            pool.join(timeout=0.1)
            pool.mode, pool.member, pool.tp_size = "shared", member, self.tp_size
            self.pools.append(pool)
            self.addCleanup(pool.close, drain=lambda: None)
        for side in (0, 1):
            layout = self.layout.layout_for_rank(side)
            for layer in range(self.tp_size):
                self.memory[self.pools[0].view(side, layer).device_base] = torch.stack(
                    [
                        kv_pattern(side, layer, slot, 0, layout.tokens, 1, layout.dim)
                        for slot in range(layout.slots)
                    ]
                )

    def kernel(self, src, dst, si, di, valid, sr, dr, size, count, blocks, sp, dp):
        source = (self.memory[sp] if sp is not None else src).reshape(sr, size // 2)
        target = (self.memory[dp] if dp is not None else dst).reshape(dr, size // 2)
        target[di[valid]] = source[si[valid]]

    def work(self, case, member):
        return CopyWorkload(
            self.pools[member],
            case,
            topk=8,
            block_dim=24,
            device="cpu",
            kernel=self.kernel,
        )

    def test_shared_token_feature_and_layer_writes_preserve_guards(self):
        for partition, layer in [("token", 0), ("feature", 0)] + [
            ("layer", layer) for layer in range(self.tp_size)
        ]:
            with self.subTest(partition=partition):
                work = [
                    self.work(
                        Case(
                            "write", "logical", rows=5, partition=partition, layer=layer
                        ),
                        member,
                    )
                    for member in range(self.tp_size)
                ]
                for item in work:
                    item.write()
                for item in work:
                    item.verify_writes(torch)
                # A missing follower half must be observable, even though the
                # first feature was written correctly by the owner.
                if partition == "feature":
                    address = self.pools[0].view(1, 0).device_base
                    self.memory[address][1, work[0].first_write, 0, -1] = 123
                    with self.assertRaises(AssertionError):
                        work[0].verify_writes(torch)

    def test_both_readers_get_the_full_selection_after_slot_and_index_switch(self):
        work = [
            self.work(Case("read", "read", active=self.tp_size), member)
            for member in range(self.tp_size)
        ]
        for variant in (0, 1, 0):
            for item in work:
                item.load(variant)
                item.history()
                item.verify_selected()
                self.assertEqual(
                    item.byte_counts()["bm_read_bytes"], self.tp_size * 8 * 1152
                )
                item.load(variant, empty=True)
                item.history()
                self.assertTrue(torch.equal(item.output, torch.zeros_like(item.output)))

    def test_current_fill_and_history_are_disjoint_before_peer_write_completes(self):
        work = [
            self.work(Case("selected", "current_parallel", active=self.tp_size), member)
            for member in range(self.tp_size)
        ]
        for item in work:
            item.current()
            item.history()
            item.verify_selected()
        # Complete the two writes only after selected KV has been consumed.
        for item in work:
            item.write()
        for item in work:
            item.verify_writes(torch)

    def test_sequential_baseline_reads_same_current_kv_from_bm(self):
        baseline = [
            self.work(
                Case("sequential", "write_then_read", active=self.tp_size), member
            )
            for member in range(self.tp_size)
        ]
        overlap = self.work(
            Case("overlap", "write_with_history", active=self.tp_size), 0
        )
        self.assertTrue(torch.equal(baseline[0].host_positions, overlap.host_positions))
        self.assertTrue(torch.equal(baseline[0].expected, overlap.expected))
        for item in baseline[:-1]:
            item.write()
        baseline[0].history()
        with self.assertRaises(AssertionError):
            baseline[0].verify_selected()
        baseline[-1].write()
        for item in baseline:
            item.history()
            item.verify_selected()
            self.assertEqual(
                item.byte_counts()["bm_read_bytes"], self.tp_size * 8 * 1152
            )
            self.assertEqual(item.byte_counts()["current_bytes"], 0)
        self.assertEqual(
            overlap.byte_counts()["bm_read_bytes"], self.tp_size * 6 * 1152
        )
        self.assertEqual(
            overlap.byte_counts()["current_bytes"], self.tp_size * 2 * 1152
        )

    def test_idle_consumer_side_issues_no_payload(self):
        item = CopyWorkload(
            self.pools[0],
            Case("idle", "write_with_history", active=self.tp_size),
            topk=8,
            block_dim=24,
            device="cpu",
            kernel=self.kernel,
            side_active=False,
        )
        before = {address: value.clone() for address, value in self.memory.items()}
        item.write()
        item.history()
        item.current()
        item.verify_selected()
        item.verify_writes(torch)
        self.assertEqual(item.byte_counts()["copy_bytes"], 0)
        self.assertEqual(item.byte_counts()["logical_bytes"], 0)
        for address, value in self.memory.items():
            self.assertTrue(torch.equal(value, before[address]))

    def test_equal_bytes_uses_disjoint_ranges_without_changing_per_rank_payload(self):
        work = [
            self.work(Case("equal", "equal_bytes", rows=5), m)
            for m in range(self.tp_size)
        ]
        rows = [set(item.write_tokens_host.tolist()) for item in work]
        self.assertEqual(len(set.union(*rows)), self.tp_size * 5)
        for item in work:
            item.write()
        for item in work:
            item.verify_writes(torch)
            self.assertEqual(item.byte_counts()["write_bytes"], 5 * 1152)
        logical = [
            self.work(Case("logical", "logical", rows=5), m)
            for m in range(self.tp_size)
        ]
        self.assertEqual(sum(w.byte_counts()["write_bytes"] for w in logical), 5 * 1152)

    def test_single_reader_can_select_each_follower_without_extra_copies(self):
        for accessor in range(self.tp_size):
            work = [
                self.work(Case("single", "read", accessor=accessor), m)
                for m in range(self.tp_size)
            ]
            for member, item in enumerate(work):
                item.history()
                item.verify_selected()
                self.assertEqual(
                    item.byte_counts()["bm_read_bytes"],
                    8 * 1152 if member == accessor else 0,
                )


class TestTP4Copies(TestSharedCopies):
    tp_size = 4


if __name__ == "__main__":
    unittest.main()
