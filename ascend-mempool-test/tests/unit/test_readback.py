"""Exercise real KV comparison through runtime, SDK copy and completion seams."""

import unittest
from types import SimpleNamespace

import torch
from test_offload import CPUWriteKernel
from test_pool import FakeBM
from test_runtime import ManualEvent, prefill_batch

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime


class TestKVReadback(unittest.TestCase):
    """Use independent old-path values; substitute only hardware operations."""

    def setUp(self):
        """Provide two layers of actual CPU source storage behind fake BM pointers."""
        layout = KVLayout(layers=2, slots=16, tokens=8, heads=1, dim=4)
        manager = MempoolKVManager.create(
            PoolLayout(layout, layout), rank=1, bm_module=FakeBM(1)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        self.p = [
            torch.full(layout.shape, -100, dtype=torch.bfloat16) for _ in range(2)
        ]
        self.d = [torch.full_like(value, -200) for value in self.p]
        sources = {
            manager.view(rank, layer).device_base: buffers[layer]
            for rank, buffers in ((0, self.p), (1, self.d))
            for layer in range(2)
        }
        self.copy_counts = []

        def copy(src, dst, si, di, valid, sr, dr, rb, count, block, sp, dp):
            """Implement the SDK's indexed copy without sharing routing logic."""
            self.copy_counts.append(int(valid.sum()))
            dst.reshape(dr, rb // 2)[di[valid]] = sources[sp].reshape(sr, rb // 2)[
                si[valid]
            ]

        self.events = []

        def event():
            """Keep completion controlled independently from immediate CPU work."""
            value = ManualEvent()
            self.events.append(value)
            return value

        self.runtime = MempoolRuntime(
            manager,
            req_pool_rows=9,
            max_context_len=16,
            start_layer=5,
            device="cpu",
            event_factory=event,
            readback_enabled=True,
            readback_kernel=copy,
            kernels=[
                CPUWriteKernel(self.d[i], manager.view(1, i).device_base)
                for i in range(2)
            ],
        )

    def forward(self, positions, references, *, offset=0, compare_layers=(5, 6)):
        """Submit one real decode row plus a graph padding row through public hooks."""
        size = positions.shape[0]
        batch = prefill_batch((1,) * size, (0,) * size, (1,) + (0,) * (size - 1))
        batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
        batch.seq_lens = torch.tensor([5 + offset] + [0] * (size - 1))
        self.runtime.begin_forward([KVWriteExpectation(1, 4 + offset, 1)])
        for i in range(2):
            k = torch.full((size, 2), 201 + offset, dtype=torch.bfloat16)
            self.runtime.write_layer(5 + i, k, k, batch)
            if 5 + i in compare_layers:
                self.runtime.compare_selected_kv(
                    5 + i, batch.req_pool_indices, positions, references[i]
                )
        self.runtime.end_forward()
        return self.events[-1]

    def test_boundary_unequal_slots_padding_and_device_completion(self):
        """D offset zero reads local KV; padding/NaNs cannot pollute valid comparisons."""
        binding = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        for source in self.p:
            source[2, 0] = 101
            source[2, 3] = 103
        positions = torch.tensor([[0, 3, 4, -1, -1], [0, 1, 2, 3, 4]])
        reference = torch.full((2, 5, 1, 4), float("nan"), dtype=torch.bfloat16)
        reference[0, 0], reference[0, 1], reference[0, 2] = 101, 103, 201
        original = reference.clone()
        event = self.forward(positions, [reference, reference])
        self.assertEqual(self.runtime.poll_completed(), [])
        with self.assertRaisesRegex(RuntimeError, "in-flight"):
            self.runtime.readback_report(binding)
        event.done = True
        self.runtime.poll_completed()
        report = self.runtime.readback_report(binding)
        self.assertEqual(report["forwards"], 1)
        self.assertEqual(report["layers"], 2)
        self.assertEqual(report["prompt_kv"], 4)
        self.assertEqual(report["decode_kv"], 2)
        self.assertEqual(report["prompt_boundary_kv"], 2)
        self.assertEqual(report["decode_first_kv"], 2)
        self.assertEqual(report["min_valid_per_layer"], 3)
        self.assertEqual(report["written_kv"], 1)
        self.assertEqual(
            (report["row"], report["prompt_slot"], report["decode_slot"]), (1, 2, 3)
        )
        self.assertTrue(torch.allclose(reference, original, equal_nan=True))

    def test_requires_a_peer_slot_for_readback_binding(self):
        """D cannot infer P ownership from the local slot or request row."""
        with self.assertRaisesRegex(ValueError, "prompt slot"):
            self.runtime.bind(1, slot=3, prompt_tokens=4)

    def test_mismatch_reports_layer_row_position_and_blocks_release(self):
        """Corrupt one old-path element and fail only when the completion is visible."""
        binding = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        reference = torch.full((1, 1, 1, 4), 201, dtype=torch.bfloat16)
        wrong = reference.clone()
        wrong[0, 0, 0, 2] = 999
        event = self.forward(torch.tensor([[4]]), [reference, wrong])
        self.assertEqual(self.runtime.poll_completed(), [])
        event.done = True
        with self.assertRaisesRegex(
            RuntimeError, r"layer=6 row=1 position=4 feature=2"
        ):
            self.runtime.poll_completed()
        self.assertEqual(self.runtime.written_tokens(1), 0)
        with self.assertRaisesRegex(RuntimeError, "fault"):
            self.runtime.detach_row(binding)
        with self.assertRaisesRegex(RuntimeError, "fault"):
            self.runtime.begin_forward([KVWriteExpectation(1, 5, 1)])

    def test_positive_unwritten_position_fails_instead_of_becoming_padding(self):
        """A sampled token whose KV was never written cannot make a successful check."""
        self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        reference = torch.full((1, 1, 1, 4), 201, dtype=torch.bfloat16)
        event = self.forward(torch.tensor([[5]]), [reference, reference])
        self.assertEqual(self.copy_counts, [0, 0, 0, 0])
        event.done = True
        with self.assertRaisesRegex(RuntimeError, r"position=5.*invalid_kv=1"):
            self.runtime.poll_completed()

    def test_overlap_keeps_a_failed_snapshot_when_next_forward_matches(self):
        """A later successful selection must not overwrite an earlier comparison."""
        self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        wrong = torch.zeros((1, 1, 1, 4), dtype=torch.bfloat16)
        first = self.forward(torch.tensor([[4]]), [wrong, wrong])
        correct = torch.full_like(wrong, 202)
        second = self.forward(torch.tensor([[5]]), [correct, correct], offset=1)
        second.done = True
        self.assertEqual(self.runtime.poll_completed(), [])
        first.done = True
        with self.assertRaisesRegex(RuntimeError, r"position=4.*mismatch_kv=1"):
            self.runtime.poll_completed()

    def test_2048_width_and_row_reuse_keep_evidence_separate(self):
        """A full graph shape counts only valid KV and forgets the previous attachment."""
        old = self.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        positions = torch.full((16, 2048), -1, dtype=torch.int64)
        positions[0, 0] = 4
        reference = torch.full((16, 2048, 1, 4), float("nan"), dtype=torch.bfloat16)
        reference[0, 0] = 201
        event = self.forward(positions, [reference, reference])
        event.done = True
        self.runtime.poll_completed()
        report = self.runtime.readback_report(old)
        self.assertEqual((report["prompt_kv"], report["decode_kv"]), (0, 2))
        self.assertEqual((report["min_topk"], report["max_topk"]), (2048, 2048))
        self.assertEqual(report["min_valid_per_layer"], 1)
        self.runtime.detach_row(old)
        current = self.runtime.bind(1, slot=5, prompt_tokens=4, prompt_slot=4)
        for source in self.p:
            source[4, 0] = 301
        positions[0, 0] = 0
        reference[0, 0] = 301
        event = self.forward(positions, [reference, reference])
        event.done = True
        self.runtime.poll_completed()
        new = self.runtime.readback_report(current)
        self.assertEqual(new["forwards"], 1)
        self.assertEqual((new["prompt_kv"], new["decode_kv"]), (2, 0))
        self.assertEqual(report["decode_kv"], 2)
        with self.assertRaisesRegex(RuntimeError, "binding"):
            self.runtime.readback_report(old)

    def test_capture_keeps_zero_valid_sources_and_requires_all_layers(self):
        """Unbound warmup records both source kernels without claiming real comparisons."""
        batch = prefill_batch((1,), (0,), (0,))
        batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
        batch.seq_lens = torch.tensor([0])
        self.runtime.begin_forward([], capture=True)
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        ref = torch.full((1, 1, 1, 4), float("nan"), dtype=torch.bfloat16)
        for layer in (5, 6):
            self.runtime.write_layer(layer, k, k, batch)
        self.runtime.compare_selected_kv(
            5, batch.req_pool_indices, torch.tensor([[0]]), ref
        )
        with self.assertRaisesRegex(RuntimeError, "missing local layer readbacks"):
            self.runtime.end_forward()
        self.runtime.compare_selected_kv(
            6, batch.req_pool_indices, torch.tensor([[0]]), ref
        )
        self.runtime.end_forward()
        self.assertEqual(self.copy_counts, [0, 0, 0, 0])
        self.assertEqual(self.runtime.poll_completed(), [])
        self.assertEqual(self.runtime.readback_report(None)["status"], "zero_decode")


if __name__ == "__main__":
    unittest.main()
