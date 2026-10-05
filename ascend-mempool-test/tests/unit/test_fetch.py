"""Exercise production BM fetch through runtime with CPU device operations."""

import unittest
import weakref
from types import SimpleNamespace

import torch
from test_offload import CPUWriteKernel
from test_pool import FakeBM
from test_runtime import ManualEvent, prefill_batch

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime


class TestKVFetch(unittest.TestCase):
    """Keep binding, range validation, routing and completion code real."""

    def setUp(self):
        layout = PoolLayout(
            KVLayout(layers=1, slots=16, tokens=8, heads=1, dim=4),
            KVLayout(layers=1, slots=16, tokens=16, heads=1, dim=4),
        )
        manager = MempoolKVManager.create(layout, 1, bm_module=FakeBM(1))
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        self.p = torch.full(layout.prompt.shape, -100, dtype=torch.bfloat16)
        self.d = torch.full(layout.decode.shape, -200, dtype=torch.bfloat16)
        self.sources = {
            manager.view(0, 0).device_base: self.p,
            manager.view(1, 0).device_base: self.d,
        }
        self.copy_counts = []

        def copy(src, dst, si, di, valid, sr, dr, rb, count, block, sp, dp):
            self.copy_counts.append(int(valid.sum()))
            dst.reshape(dr, rb // 2)[di[valid]] = self.sources[sp].reshape(sr, rb // 2)[
                si[valid]
            ]

        self.events = []

        def event():
            value = ManualEvent()
            self.events.append(value)
            return value

        self.runtime = MempoolRuntime(
            manager,
            req_pool_rows=9,
            max_context_len=24,
            start_layer=5,
            device="cpu",
            fetch_enabled=True,
            fetch_kernel=copy,
            event_factory=event,
            kernels=[CPUWriteKernel(self.d, manager.view(1, 0).device_base)],
        )

    def start(self, rows=(1, 0), offset=0):
        batch = prefill_batch((1,) * len(rows), (0,) * len(rows), rows)
        batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
        batch.seq_lens = torch.tensor([5 + offset if row else 0 for row in rows])
        self.runtime.begin_forward([KVWriteExpectation(1, 4 + offset, 1)])
        k = torch.full((len(rows), 2), 201 + offset, dtype=torch.bfloat16)
        self.runtime.write_layer(5, k, k, batch)
        return batch

    def test_fetch_without_readback_preserves_hits_and_uses_independent_slots(self):
        binding = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        self.p[2, 0] = 101
        self.p[2, 3] = 103
        batch = self.start()
        positions = torch.tensor([[0, 3, 4, -1], [0, 3, 4, -1]])
        valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
        self.assertEqual(valid.tolist(), [[True, True, True, False], [False] * 4])
        output = torch.full((2, 4, 1, 4), -7, dtype=torch.bfloat16)
        output[0, 1] = 77  # Already supplied by the HBM hit stream.
        misses = valid.clone()
        misses[0, 1] = False
        self.runtime.fetch_selected_kv(
            5, batch.req_pool_indices, positions, misses, output
        )
        self.assertEqual(output[0, :, 0, 0].tolist(), [101, 77, 201, -7])
        self.assertTrue((output[1] == -7).all())
        self.runtime.end_forward()
        self.assertEqual(self.runtime.poll_completed(), [])
        self.events[-1].done = True
        self.runtime.poll_completed()
        self.assertEqual(self.runtime.written_tokens(1), 1)
        self.assertIsNone(self.runtime.readback_report(binding))
        self.runtime.detach_row(binding)

    def test_unwritten_nonnegative_selection_faults_after_completion(self):
        binding = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        batch = self.start()
        positions = torch.tensor([[5, -1], [99, -1]])
        valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
        output = torch.full((2, 2, 1, 4), -7, dtype=torch.bfloat16)
        self.runtime.fetch_selected_kv(
            5, batch.req_pool_indices, positions, valid, output
        )
        self.assertFalse(valid.any())
        self.assertTrue((output == -7).all())
        self.runtime.end_forward()
        self.assertEqual(self.runtime.poll_completed(), [])
        self.events[-1].done = True
        with self.assertRaisesRegex(RuntimeError, r"layer=5 row=1 position=5"):
            self.runtime.poll_completed()
        with self.assertRaisesRegex(RuntimeError, "fault"):
            self.runtime.detach_row(binding)

    def test_completion_report_waits_for_all_forwards_and_resets_on_reuse(self):
        binding = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        self.p[2, 0] = 101
        for step in range(2):
            batch = self.start(offset=step)
            positions = torch.tensor([[0, 4, -1], [-1, -1, -1]])
            valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
            misses = valid if step == 0 else torch.zeros_like(valid)
            self.runtime.fetch_selected_kv(
                5,
                batch.req_pool_indices,
                positions,
                misses,
                torch.zeros((2, 3, 1, 4), dtype=torch.bfloat16),
            )
            self.runtime.end_forward()
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            self.runtime.fetch_report(binding)
        self.events[-2].done = True
        self.runtime.poll_completed()
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            self.runtime.fetch_report(binding)
        self.events[-1].done = True
        self.runtime.poll_completed()
        report = self.runtime.fetch_report(binding)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["forwards"], 2)
        self.assertEqual(report["written_kv"], 2)
        self.assertEqual(report["layer_checks"], 2)
        self.assertEqual(report["selected_kv"], 4)
        self.assertEqual(report["cache_hits"], 2)
        self.assertEqual((report["prompt_misses"], report["decode_misses"]), (1, 1))
        self.runtime.detach_row(binding)
        new_binding = self.runtime.bind(1, slot=5, prompt_tokens=2, prompt_slot=7)
        empty = self.runtime.fetch_report(new_binding)
        self.assertEqual(
            (empty["status"], empty["forwards"], empty["selected_kv"]),
            ("zero_decode", 0, 0),
        )
        self.assertEqual((empty["prompt_slot"], empty["decode_slot"]), (7, 5))
        with self.assertRaisesRegex(RuntimeError, "binding"):
            self.runtime.fetch_report(binding)

    def test_new_destination_and_rebound_row_do_not_reuse_old_copy_target(self):
        old = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        outputs = []
        for step in range(2):
            if step:
                self.runtime.detach_row(old)
                self.runtime.bind(1, slot=5, prompt_tokens=4, prompt_slot=7)
            self.p[2 if step == 0 else 7, 0] = 101 + step
            batch = self.start()
            positions = torch.tensor([[0, -1], [0, -1]])
            valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
            output = torch.full((2, 2, 1, 4), -7, dtype=torch.bfloat16)
            self.runtime.fetch_selected_kv(
                5, batch.req_pool_indices, positions, valid, output
            )
            if step == 0:
                # A graph may still refer to metadata allocated during warmup.
                captured_inputs = weakref.ref(
                    self.runtime._fetch._copies[(0, 2, 2)].inputs
                )
            else:
                self.assertIsNotNone(captured_inputs())
                self.assertIs(
                    self.runtime._fetch._copies[(0, 2, 2)].inputs, captured_inputs()
                )
            outputs.append(output)
            self.runtime.end_forward()
            self.events[-1].done = True
            self.runtime.poll_completed()
        self.assertEqual([o[0, 0, 0, 0].item() for o in outputs], [101, 102])
        self.assertEqual(self.copy_counts, [1, 0, 1, 0])

    def test_capture_retains_two_empty_copies_and_requires_fetch_coverage(self):
        batch = prefill_batch((1,), (0,), (0,))
        batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
        batch.seq_lens = torch.tensor([0])
        self.runtime.begin_forward([], capture=True)
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        self.runtime.write_layer(5, k, k, batch)
        with self.assertRaisesRegex(RuntimeError, "missing local layer fetches"):
            self.runtime.end_forward()
        positions = torch.tensor([[0, -1]])
        valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
        output = torch.full((1, 2, 1, 4), -7, dtype=torch.bfloat16)
        self.runtime.fetch_selected_kv(
            5, batch.req_pool_indices, positions, valid, output
        )
        self.runtime.end_forward()
        self.assertEqual(self.copy_counts, [0, 0])
        self.assertTrue((output == -7).all())
        self.assertEqual(self.runtime.poll_completed(), [])

    def test_decode_binding_requires_prompt_slot_with_readback_off(self):
        with self.assertRaisesRegex(ValueError, "prompt slot"):
            self.runtime.bind(1, slot=3, prompt_tokens=4)
        with self.assertRaisesRegex(IndexError, "prompt slot"):
            self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=16)

    def test_later_valid_forward_cannot_erase_pending_fetch_error(self):
        self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        for offset, position in enumerate((5, 4)):
            batch = self.start(offset=offset)
            positions = torch.tensor([[position], [-1]])
            valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
            output = torch.zeros((2, 1, 1, 4), dtype=torch.bfloat16)
            self.runtime.fetch_selected_kv(
                5, batch.req_pool_indices, positions, valid, output
            )
            self.runtime.end_forward()
            if offset == 0:
                first = self.events[-1]
        self.events[-1].done = True
        self.assertEqual(self.runtime.poll_completed(), [])
        first.done = True
        with self.assertRaisesRegex(RuntimeError, r"position=5 invalid_kv=1"):
            self.runtime.poll_completed()

    def test_bound_row_absent_from_selection_is_not_successful_fetch(self):
        self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        self.start()
        rows, positions = torch.tensor([2, 0]), torch.tensor([[0], [0]])
        valid = self.runtime.selected_kv_valid(5, rows, positions)
        self.runtime.fetch_selected_kv(
            5, rows, positions, valid, torch.zeros((2, 1, 1, 4), dtype=torch.bfloat16)
        )
        self.runtime.end_forward()
        self.events[-1].done = True
        with self.assertRaisesRegex(RuntimeError, r"row=1.*checks=0 expected=1"):
            self.runtime.poll_completed()

    def test_stale_decode_position_does_not_publish_unwritten_kv(self):
        self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        batch = self.start()
        positions = torch.tensor([[4], [-1]])
        output = torch.full((2, 1, 1, 4), -7, dtype=torch.bfloat16)
        valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
        self.runtime.fetch_selected_kv(
            5, batch.req_pool_indices, positions, valid, output
        )
        self.runtime.end_forward()
        self.events[-1].done = True
        self.runtime.poll_completed()

        # The host expects offset 1, but stale graph metadata would write offset 0 again.
        self.runtime.begin_forward([KVWriteExpectation(1, 5, 1)])
        k = torch.full((2, 2), 202, dtype=torch.bfloat16)
        self.runtime.write_layer(5, k, k, batch)
        positions[0, 0] = 5
        output.fill_(-7)
        valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
        self.assertFalse(valid.any())
        self.runtime.fetch_selected_kv(
            5, batch.req_pool_indices, positions, valid, output
        )
        self.assertTrue((output == -7).all())
        self.assertEqual(self.d[3, 0, 0, 0].item(), 201)
        self.runtime.end_forward()
        failed_event = self.events[-1]

        # Another queued forward must not turn the skipped position into a
        # readable prefix before the prior failure reaches host completion.
        self.runtime.begin_forward([KVWriteExpectation(1, 6, 1)])
        batch.seq_lens[0] = 7
        self.runtime.write_layer(5, k, k, batch)
        valid = self.runtime.selected_kv_valid(5, batch.req_pool_indices, positions)
        self.assertFalse(valid.any())
        self.runtime.fetch_selected_kv(
            5, batch.req_pool_indices, positions, valid, output
        )
        self.assertTrue((output == -7).all())
        self.runtime.end_forward()
        self.events[-1].done = True
        failed_event.done = True
        with self.assertRaisesRegex(RuntimeError, "write counts"):
            self.runtime.poll_completed()


if __name__ == "__main__":
    unittest.main()
