"""Check forward writes and ownership adapters through a CPU kernel boundary."""

import unittest
from types import SimpleNamespace

import torch
from msgspec.structs import replace
from test_offload import CPUWriteKernel
from test_pool import FakeBM

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime
from ascend_mempool.writer_cases import WRITER_SENTINEL, decode_cases, prefill_cases
from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_protocol import PoolDescriptor, PoolPeer


class ManualEvent:
    """Expose device completion explicitly without mocking runtime collaborators."""

    def __init__(self):
        """Start with device work outstanding."""
        self.done = False

    def record(self):
        """CPU operations are immediate, but the test controls observed completion."""

    def query(self):
        """Report whether the test has drained the external device."""
        return self.done


def prefill_batch(lengths=(2, 1), prefixes=(0, 0), rows=(1, 2)):
    """Build just the forward fields consumed by the public runtime interface."""
    return SimpleNamespace(
        forward_mode=SimpleNamespace(is_decode=lambda: False),
        req_pool_indices=torch.tensor(rows),
        seq_lens=None,
        extend_seq_lens=torch.tensor(lengths),
        extend_prefix_lens=torch.tensor(prefixes),
        extend_seq_lens_cpu=list(lengths),
        global_num_token_non_padded_cpu=None,
        out_cache_loc=torch.arange(sum(lengths)),
    )


class TestMempoolRuntime(unittest.TestCase):
    """Use real binding/index/counter logic with fake BM and device completion."""

    def make_runtime(self, rank=0, layers=2, **kwargs):
        """Open small local storage and inject only SDK/kernel/event boundaries."""
        layout = KVLayout(layers=layers, slots=16, tokens=8, heads=1, dim=4)
        manager = MempoolKVManager.create(
            PoolLayout(layout, layout), rank, bm_module=FakeBM(rank)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        targets = [
            torch.full(layout.shape, -1, dtype=torch.bfloat16) for _ in range(layers)
        ]
        kernels = [
            CPUWriteKernel(target, manager.view(rank, i).device_base)
            for i, target in enumerate(targets)
        ]
        events = []

        def event_factory():
            """Create an observable SDK event for binding or write completion."""
            event = ManualEvent()
            events.append(event)
            return event

        runtime = MempoolRuntime(
            manager,
            req_pool_rows=9,
            max_context_len=16,
            start_layer=5,
            device="cpu",
            kernels=kernels,
            event_factory=event_factory,
            **kwargs,
        )
        return runtime, targets, events

    def test_forward_scope_requires_preparation_and_keeps_exceptions_fatal(self):
        """Host scopes record real forwards once and cannot disguise failed launches."""
        runtime, _, events = self.make_runtime(layers=1)
        binding = runtime.bind(1, slot=2, prompt_tokens=2)
        runtime.prepare_forward([KVWriteExpectation(1, 0, 1)])
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        with runtime.forward_scope():
            runtime.write_layer(5, k, k, prefill_batch((1,), (0,), (1,)))
        events[-1].done = True
        runtime.poll_completed()
        self.assertEqual(runtime.written_tokens(1), 1)
        runtime.prepare_forward([KVWriteExpectation(1, 1, 1)])
        with self.assertRaisesRegex(RuntimeError, "device failure"):
            with runtime.forward_scope():
                raise RuntimeError("device failure")
        self.assertIn("device failure", runtime.fault)
        self.assertEqual(runtime.written_tokens(1), 1)
        with self.assertRaisesRegex(RuntimeError, "fault"):
            runtime.detach_row(binding)

    def test_detach_preserves_completed_facts_before_request_row_reuse(self):
        """Consume local completion before detaching; retain old KV after row reuse."""
        runtime, targets, events = self.make_runtime(layers=1)
        binding = runtime.bind(1, slot=2, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        k = torch.full((1, 2), 11, dtype=torch.bfloat16)
        runtime.write_layer(5, k, k, prefill_batch((1,), (0,), (1,)))
        runtime.end_forward()
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            runtime.detach_row(binding)
        events[-1].done = True
        # A ready device event still must be consumed before the row can change.
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            runtime.detach_row(binding)
        runtime.poll_completed()
        receipt = runtime.detach_row(binding)
        self.assertIs(receipt.binding, binding)
        self.assertEqual((receipt.submitted, receipt.completed), (1, 1))
        with self.assertRaises(AttributeError):
            receipt.completed = 99
        self.assertEqual(runtime.row_slot[1].item(), -1)
        self.assertEqual(runtime.row_prompt_len[1].item(), -1)

        runtime.bind(1, slot=3, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        runtime.write_layer(5, k * 2, k * 2, prefill_batch((1,), (0,), (1,)))
        runtime.end_forward()
        events[-1].done = True
        runtime.poll_completed()
        self.assertEqual(receipt.completed, 1)
        self.assertEqual(targets[0][2, 0, 0].tolist(), [11] * 4)
        self.assertEqual(targets[0][3, 0, 0].tolist(), [22] * 4)

    def test_admission_requires_the_approved_attachment_for_the_actual_req_row(self):
        """Project kv.req_pool_idx outside runtime; reject missing or stale approval."""
        runtime, _, _ = self.make_runtime()
        req = SimpleNamespace(kv=SimpleNamespace(req_pool_idx=1), rid="real")
        with self.assertRaisesRegex(RuntimeError, "binding"):
            runtime.assert_bound(req.kv.req_pool_idx, None)
        old = runtime.bind(req.kv.req_pool_idx, slot=2, prompt_tokens=1)
        runtime.assert_bound(req.kv.req_pool_idx, old)
        with self.assertRaises(AttributeError):
            old.slot = 3
        with self.assertRaisesRegex(RuntimeError, "binding"):
            runtime.assert_bound(2, old)
        with self.assertRaisesRegex(RuntimeError, "binding"):
            runtime.assert_bound(req.kv.req_pool_idx, replace(old))

        runtime.detach_row(old)
        current = runtime.bind(req.kv.req_pool_idx, slot=2, prompt_tokens=1)
        with self.assertRaisesRegex(RuntimeError, "binding"):
            runtime.assert_bound(req.kv.req_pool_idx, old)
        with self.assertRaisesRegex(RuntimeError, "binding"):
            runtime.detach_row(old)
        runtime.assert_bound(req.kv.req_pool_idx, current)
        # A real request without approval must fail even if its row is bound.
        with self.assertRaisesRegex(RuntimeError, "binding"):
            runtime.assert_bound(req.kv.req_pool_idx, None)
        other, _, _ = self.make_runtime()
        other.bind(req.kv.req_pool_idx, slot=2, prompt_tokens=1)
        with self.assertRaisesRegex(RuntimeError, "binding"):
            other.assert_bound(req.kv.req_pool_idx, current)

    def test_prefill_row_reuse_keeps_old_slot_until_its_own_done(self):
        """A controlled caller separates native row reuse from protocol ownership."""
        runtime, targets, events = self.make_runtime(layers=1)
        descriptor = PoolDescriptor(
            layers=1,
            slots=16,
            prompt_tokens=8,
            decode_tokens=8,
            heads=1,
            dim=4,
            dtype="bfloat16",
            prompt_bytes=1 << 30,
            decode_bytes=1 << 30,
            stride_bytes=1 << 30,
        )
        p = MempoolPDControl(PoolPeer("p", "prefill", 0, 16, 1, 7, descriptor))
        d = MempoolPDControl(PoolPeer("d", "decode", 0, 16, 1, 7, descriptor))
        d.apply(p.apply(d.begin_handshake("tcp://d:4351")))
        first = d.acquire_decode(27, "old", 5, 1, 1, "tcp://d:4351")
        p.apply(first)
        p.apply(d.apply(p.acquire_prefill(first.request, 2)))
        p.start_prefill(first.request)
        old = runtime.bind(1, slot=2, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        k = torch.full((1, 2), 11, dtype=torch.bfloat16)
        runtime.write_layer(5, k, k, prefill_batch((1,), (0,), (1,)))
        runtime.end_forward()
        events[-1].done = True
        runtime.poll_completed()
        self.assertTrue(runtime.prompt_ready(1))
        p.finish_prefill_writes(first.request)
        d.apply(p.publish_kv_ready(first.request, 1))
        d.transfer_succeeded(first.request)
        d.start_decode(first.request)

        # Stand in for approved native handoff cleanup; the real gate belongs to IV.
        receipts = {first.request: runtime.detach_row(old)}
        self.assertNotIn(2, p.available_slots())
        self.assertEqual(p.state(first.request), "WAITING_DONE")
        second = d.acquire_decode(28, "new-row-owner", 6, 1, 1, "tcp://d:4351")
        p.apply(second)
        with self.assertRaisesRegex(ValueError, "already acquired"):
            p.acquire_prefill(second.request, 2)
        p.apply(d.apply(p.acquire_prefill(second.request, 3)))
        p.start_prefill(second.request)
        current = runtime.bind(1, slot=3, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        runtime.write_layer(5, k * 2, k * 2, prefill_batch((1,), (0,), (1,)))
        runtime.end_forward()

        d.begin_drain(first.request)
        done = d.finish_drain(first.request)
        ack = p.apply(done)
        d.apply(ack)
        self.assertIn(2, p.available_slots())
        self.assertNotIn(3, p.available_slots())
        runtime.assert_bound(1, current)
        self.assertFalse(runtime.writes_done(1))
        self.assertEqual(runtime.written_tokens(1), 0)
        self.assertEqual(receipts[first.request].completed, 1)
        self.assertEqual(targets[0][2, 0, 0].tolist(), [11] * 4)
        self.assertEqual(targets[0][3, 0, 0].tolist(), [22] * 4)

        third = d.acquire_decode(29, "new-slot-owner", 7, 1, 1, "tcp://d:4351")
        p.apply(third)
        p.acquire_prefill(third.request, 2)
        self.assertEqual(p.apply(done), ack)
        self.assertNotIn(2, p.available_slots())
        self.assertEqual(p.state(third.request), "ACQUIRED")
        self.assertEqual(p.state(second.request), "PREFILLING")
        events[-1].done = True
        runtime.poll_completed()
        self.assertTrue(runtime.prompt_ready(1))
        self.assertEqual(receipts[first.request].binding.slot, 2)

    def test_prompt_chunks_and_layers_are_counted_once_after_device_completion(self):
        """Two forwards fill prompt KV, and readiness waits for all layer writes."""
        runtime, targets, events = self.make_runtime()
        binding = runtime.bind(1, slot=3, prompt_tokens=3)
        runtime.bind(2, slot=7, prompt_tokens=1)
        batch = prefill_batch()
        runtime.begin_forward(
            [KVWriteExpectation(1, 0, 2), KVWriteExpectation(2, 0, 1)]
        )
        k = torch.tensor([[11, 12], [21, 22], [31, 32]], dtype=torch.bfloat16)
        pe = k + 2
        for layer in (5, 6):
            runtime.write_layer(layer, k, pe, batch)
        runtime.end_forward()
        self.assertEqual(runtime.poll_completed(), [])
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            runtime.detach_row(binding)
        events[-1].done = True
        self.assertEqual(runtime.poll_completed(), [1, 2])
        self.assertFalse(runtime.prompt_ready(1))
        self.assertTrue(runtime.prompt_ready(2))

        runtime.begin_forward([KVWriteExpectation(1, 2, 1)])
        for layer in (5, 6):
            runtime.write_layer(layer, k[:1], pe[:1], prefill_batch((1,), (2,), (1,)))
        runtime.end_forward()
        events[-1].done = True
        runtime.poll_completed()
        self.assertTrue(runtime.prompt_ready(1))
        for target in targets:
            self.assertEqual(
                target[3, :3, 0].tolist(),
                [[11, 12, 13, 14], [21, 22, 23, 24], [11, 12, 13, 14]],
            )
            self.assertEqual(target[7, 0, 0].tolist(), [31, 32, 33, 34])
            self.assertTrue((target[0] == -1).all().item())

    def test_decode_first_row_and_last_capacity_row_use_relative_positions(self):
        """Eight decode rows fill exactly one slot without touching its neighbor."""
        runtime, targets, events = self.make_runtime(rank=1, layers=1)
        runtime.bind(1, slot=15, prompt_tokens=4, prompt_slot=0)
        for offset in range(8):
            batch = prefill_batch((1,), (0,), (1,))
            batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
            batch.seq_lens = torch.tensor([5 + offset])
            runtime.begin_forward([KVWriteExpectation(1, 4 + offset, 1)])
            runtime.write_layer(
                5,
                torch.full((1, 2), offset + 10, dtype=torch.bfloat16),
                torch.full((1, 2), offset + 20, dtype=torch.bfloat16),
                batch,
            )
            runtime.end_forward()
            events[-1].done = True
            runtime.poll_completed()
        self.assertEqual(runtime.written_tokens(1), 8)
        self.assertEqual(targets[0][15, 0, 0].tolist(), [10, 10, 20, 20])
        self.assertEqual(targets[0][15, 7, 0].tolist(), [17, 17, 27, 27])
        self.assertTrue((targets[0][:15] == -1).all().item())
        with self.assertRaisesRegex(ValueError, "capacity"):
            runtime.begin_forward([KVWriteExpectation(1, 12, 1)])

    def test_binding_reuse_preserves_tensor_addresses_and_rejects_conflicts(self):
        """A drained allocation can be rebound without replacing captured tables."""
        runtime, targets, events = self.make_runtime(layers=1)
        pointers = runtime.row_slot.data_ptr(), runtime.row_prompt_len.data_ptr()
        with self.assertRaisesRegex(ValueError, "row 0"):
            runtime.bind(0, slot=1, prompt_tokens=1)
        binding = runtime.bind(1, slot=2, prompt_tokens=1)
        with self.assertRaisesRegex(RuntimeError, "slot"):
            runtime.bind(2, slot=2, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        k = torch.full((1, 2), 11, dtype=torch.bfloat16)
        runtime.write_layer(5, k, k, prefill_batch((1,), (0,), (1,)))
        runtime.end_forward()
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            runtime.detach_row(binding)
        events[-1].done = True
        runtime.poll_completed()
        runtime.detach_row(binding)
        runtime.bind(1, slot=2, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        runtime.write_layer(5, k * 2, k * 2, prefill_batch((1,), (0,), (1,)))
        runtime.end_forward()
        events[-1].done = True
        runtime.poll_completed()
        self.assertEqual(runtime.written_tokens(1), 1)
        self.assertEqual(targets[0][2, 0, 0].tolist(), [22] * 4)
        self.assertEqual(
            pointers, (runtime.row_slot.data_ptr(), runtime.row_prompt_len.data_ptr())
        )
        self.assertEqual(runtime.row_slot[0].item(), -1)

    def test_unbound_rows_are_masked_and_detected_as_missing_expected_writes(self):
        """Device masking must not make a mismatched real batch appear successful."""
        runtime, targets, events = self.make_runtime(layers=1)
        runtime.bind(1, slot=2, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        runtime.write_layer(
            5,
            torch.ones((1, 2), dtype=torch.bfloat16),
            torch.ones((1, 2), dtype=torch.bfloat16),
            prefill_batch((1,), (0,), (3,)),
        )
        runtime.end_forward()
        events[-1].done = True
        with self.assertRaisesRegex(RuntimeError, "counts"):
            runtime.poll_completed()
        self.assertTrue((targets[0] == -1).all().item())

    def test_extra_writes_to_a_bound_request_fail_expectation_check(self):
        """A layer may not silently write a request absent from the host plan."""
        runtime, _, events = self.make_runtime(layers=1)
        runtime.bind(1, slot=2, prompt_tokens=1)
        runtime.bind(2, slot=3, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        k = torch.ones((2, 2), dtype=torch.bfloat16)
        runtime.write_layer(5, k, k, prefill_batch((1, 1)))
        runtime.end_forward()
        events[-1].done = True
        with self.assertRaisesRegex(RuntimeError, "counts"):
            runtime.poll_completed()

    def test_layer_coverage_is_required_even_for_dummy_capture(self):
        """An empty expectation list cannot bypass missing/duplicate layer checks."""
        runtime, _, _ = self.make_runtime()
        runtime.begin_forward([], capture=True)
        batch = prefill_batch((0,), (0,), (0,))
        batch.out_cache_loc = torch.tensor([0])
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        runtime.write_layer(5, k, k, batch)
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            runtime.write_layer(5, k, k, batch)
        with self.assertRaisesRegex(RuntimeError, "missing"):
            runtime.end_forward()
        runtime.write_layer(6, k, k, batch)
        runtime.end_forward()

    def test_replay_requires_external_work_and_does_not_fabricate_layer_calls(self):
        """Host expectation alone cannot claim a captured write actually happened."""
        runtime, _, events = self.make_runtime(layers=1)
        runtime.begin_forward([], capture=True)
        batch = prefill_batch((0,), (0,), (0,))
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        runtime.write_layer(5, k, k, batch)
        runtime.end_forward()
        runtime.bind(1, slot=2, prompt_tokens=1)
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)], replay=True)
        runtime.end_forward()  # No actual graph launch in this CPU test.
        events[-1].done = True
        with self.assertRaisesRegex(RuntimeError, "counts"):
            runtime.poll_completed()

    def test_overlap_completion_snapshots_do_not_observe_later_writes(self):
        """Both forwards can be submitted before polling their distinct events."""
        runtime, _, events = self.make_runtime(layers=1)
        runtime.bind(1, slot=2, prompt_tokens=2)
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        completions = []
        for pos in (0, 1):
            runtime.begin_forward([KVWriteExpectation(1, pos, 1)])
            runtime.write_layer(5, k, k, prefill_batch((1,), (pos,), (1,)))
            runtime.end_forward()
            completions.append(events[-1])
        completions[0].done = True
        self.assertEqual(runtime.poll_completed(), [1])
        self.assertEqual(runtime.written_tokens(1), 1)
        self.assertFalse(runtime.writes_done(1))
        completions[1].done = True
        runtime.poll_completed()
        self.assertTrue(runtime.prompt_ready(1))

    def test_rejects_unsupported_preprocessing_and_non_contiguous_progress(self):
        """Only supported preprocessing and monotonically written KV are accepted."""
        with self.assertRaisesRegex(ValueError, "MLAPO"):
            self.make_runtime(mlapo_enabled=True)
        runtime, _, _ = self.make_runtime()
        runtime.bind(1, slot=2, prompt_tokens=2)
        with self.assertRaisesRegex(ValueError, "prefix"):
            runtime.begin_forward([KVWriteExpectation(1, 1, 1)])

    def test_binding_updates_cannot_interleave_an_open_capture(self):
        """Dummy capture also consumes binding tables and cannot change them mid-forward."""
        runtime, _, _ = self.make_runtime()
        binding = runtime.bind(1, slot=2, prompt_tokens=2)
        runtime.detach_row(binding)
        runtime.begin_forward([], capture=True)
        with self.assertRaisesRegex(RuntimeError, "open forward"):
            runtime.bind(2, slot=3, prompt_tokens=2)
        with self.assertRaisesRegex(RuntimeError, "open forward"):
            runtime.detach_row(binding)

    def test_capture_cannot_access_a_real_binding(self):
        """Graph setup must not bypass normal tracking for live request storage."""
        runtime, _, _ = self.make_runtime()
        runtime.bind(1, slot=2, prompt_tokens=2)
        with self.assertRaisesRegex(RuntimeError, "dummy"):
            runtime.begin_forward([], capture=True)

    def test_forward_waits_for_latest_binding_install_event(self):
        """The injected SDK wait orders table initialization and later rebinding."""
        waits = []
        runtime, _, events = self.make_runtime(wait_event=waits.append, layers=1)
        runtime.bind(1, slot=2, prompt_tokens=1)
        latest_install = events[-1]
        runtime.begin_forward([KVWriteExpectation(1, 0, 1)])
        self.assertEqual(waits, [latest_install])

    def test_hardware_gate_examples_match_full_dram_reference_on_cpu(self):
        """The actual gate's inputs cover both BM roles through the same runtime."""
        for rank, make_cases in ((0, prefill_cases), (1, decode_cases)):
            with self.subTest(rank=rank):
                runtime, targets, events = self.make_runtime(rank=rank)
                references = [
                    torch.full_like(target, WRITER_SENTINEL) for target in targets
                ]
                for target in targets:
                    target.fill_(WRITER_SENTINEL)
                bound = {}
                for case in make_cases(8):
                    if case.reset_bindings:
                        for binding in bound.values():
                            runtime.detach_row(binding)
                        bound.clear()
                    for row, slot, prompt in case.bindings:
                        bound[row] = runtime.bind(
                            row,
                            slot=slot,
                            prompt_tokens=prompt,
                            prompt_slot=0 if rank == 1 else None,
                        )
                    runtime.begin_forward(case.writes)
                    for layer in range(2):
                        values = case.values(layer, 4)
                        runtime.write_layer(
                            layer + 5,
                            values[:, :, :2],
                            values[:, :, 2:],
                            case.batch("cpu"),
                        )
                        case.update_reference(references[layer], layer)
                    runtime.end_forward()
                    events[-1].done = True
                    runtime.poll_completed()
                    for actual, expected in zip(targets, references):
                        self.assertTrue(torch.equal(actual, expected), case.name)


if __name__ == "__main__":
    unittest.main()
