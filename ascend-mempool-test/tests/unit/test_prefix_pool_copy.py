"""Exercise the public prefix seam with delayed library completion."""

import unittest
import test_prefix_shared
from ascend_mempool.pool_copy import CopyResult


class DelayedCopy:
    def __init__(self):
        self.tasks = {}
        self.ready = False
        self.state = "READY"

    def submit(self, task):
        if task.op_id in self.tasks:
            raise AssertionError("duplicate submission")
        self.tasks[task.op_id] = task

    def poll(self, op_id):
        return CopyResult(self.tasks[op_id], self.state if self.ready else "PENDING")

    def retire(self, op_id):
        del self.tasks[op_id]


class TestPrefixPoolCopy(unittest.TestCase):
    def test_shared_admit_fill_wait_and_coverage(self):
        fixture = test_prefix_shared.TestSharedPrefix()
        self.addCleanup(fixture.doCleanups)
        states, _ = fixture.group()
        targets = states[0][0].select_pages(32)
        receipts = []
        for store, manager, _, _ in states:
            copy = DelayedCopy()
            store.copy_executor = copy
            store.begin_request("A", 1, 80)
            args = dict(
                rid="A",
                op_id=1,
                slot=1,
                prompt_tokens=80,
                source_pages=list(range(32)),
                target_pages=targets,
            )
            self.assertIsNone(store.admit(**args))
            self.assertIsNone(store.admit(**args))
            with self.assertRaises(RuntimeError):
                store.assert_source_releasable("A")
            copy.ready = True
            receipts.append(store.admit(**args))
        self.assertEqual(
            sum(len(s.copy_executor.tasks[1].blocks) for s, *_ in states), 64
        )
        for store, manager, _, _ in states:
            store.complete_admit(1, receipts)
            store.copy_executor.ready = False
            self.assertIsNone(
                store.fill(rid="A", op_id=2, slot=1, source_pages=targets)
            )
            store.copy_executor.ready = True
            self.assertTrue(
                store.fill(rid="A", op_id=2, slot=1, source_pages=targets)["readable"]
            )
        self.assertEqual(
            sum(len(s.copy_executor.tasks[2].blocks) for s, *_ in states), 288
        )
        # Full 40-page prompt: first member owns 20 pages, not half of the 32 hits.
        self.assertEqual(len(states[1][0].copy_executor.tasks[2].blocks), 12)
        store = states[0][0]
        store.copy_executor.state = "UNKNOWN"
        with self.assertRaises(RuntimeError):
            store.fill(rid="A", op_id=3, slot=1, source_pages=targets)
        with self.assertRaises(RuntimeError):
            store.assert_source_releasable("A")
