"""Verify multi-layer BM fetch against independent values through runtime hooks."""

import unittest
from types import SimpleNamespace

import torch
from test_offload import CPUWriteKernel
from test_pool import FakeBM
from test_runtime import ManualEvent, prefill_batch

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime


class TestKVFetchLayers(unittest.TestCase):
    """Substitute only hardware operations; keep binding, fetch and completion real."""

    layer_ids = (5, 6)

    def setUp(self):
        """Provide independent CPU storage for every model layer behind BM pointers."""
        count = len(self.layer_ids)
        layout = KVLayout(layers=count, slots=16, tokens=8, heads=1, dim=4)
        manager = MempoolKVManager.create(
            PoolLayout(layout, layout), rank=1, bm_module=FakeBM(1)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        self.p = [
            torch.full(layout.shape, -100, dtype=torch.bfloat16) for _ in range(count)
        ]
        self.d = [torch.full_like(value, -200) for value in self.p]
        sources = {
            manager.view(rank, layer).device_base: buffers[layer]
            for rank, buffers in ((0, self.p), (1, self.d))
            for layer in range(count)
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
            start_layer=self.layer_ids[0],
            device="cpu",
            event_factory=event,
            fetch_enabled=True,
            source_evidence=True,
            fetch_kernel=copy,
            kernels=[
                CPUWriteKernel(self.d[i], manager.view(1, i).device_base)
                for i in range(count)
            ],
        )

    def forward(self, positions):
        size = positions.shape[0]
        batch = prefill_batch((1,) * size, (0,) * size, (1,) + (0,) * (size - 1))
        batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
        batch.seq_lens = torch.tensor([5] + [0] * (size - 1))
        outputs = []
        self.runtime.begin_forward([KVWriteExpectation(1, 4, 1)])
        for index, layer in enumerate(self.layer_ids):
            k = torch.full((size, 2), 101 + index, dtype=torch.bfloat16)
            self.runtime.write_layer(layer, k, k, batch)
            valid = self.runtime.selected_kv_valid(
                layer, batch.req_pool_indices, positions
            )
            output = torch.full((*positions.shape, 1, 4), -7, dtype=torch.bfloat16)
            self.runtime.fetch_selected_kv(
                layer, batch.req_pool_indices, positions, valid, output
            )
            outputs.append(output)
        self.runtime.end_forward()
        return self.events[-1], outputs

    def test_all_layers_keep_prompt_boundary_decode_zero_and_padding_on_reuse(self):
        positions = torch.full((16, 2048), -1, dtype=torch.int64)
        positions[0, :3] = torch.tensor([0, 3, 4])
        # Unbound graph padding must remain untouched.
        positions[1, :3] = positions[0, :3]
        old = None
        for prompt_slot, decode_slot, base in ((2, 3, 20), (4, 5, 120)):
            with self.subTest(prompt_slot=prompt_slot, decode_slot=decode_slot):
                binding = self.runtime.bind(
                    1, slot=decode_slot, prompt_tokens=4, prompt_slot=prompt_slot
                )
                if old is not None:
                    with self.assertRaisesRegex(RuntimeError, "binding"):
                        self.runtime.completion_report(old)
                for layer, source in enumerate(self.p):
                    source[prompt_slot, 0] = base + 1 + layer
                    source[prompt_slot, 3] = base + 3 + layer
                event, outputs = self.forward(positions)
                for layer, output in enumerate(outputs):
                    expected = torch.full_like(output, -7)
                    expected[0, 0] = base + 1 + layer
                    expected[0, 1] = base + 3 + layer
                    expected[0, 2] = 101 + layer
                    torch.testing.assert_close(output, expected, rtol=0, atol=0)
                self.assertEqual(self.runtime.poll_completed(), [])
                with self.assertRaisesRegex(RuntimeError, "in-flight"):
                    self.runtime.detach_row(binding)
                event.done = True
                self.runtime.poll_completed()
                report = self.runtime.completion_report(binding)
                self.assertEqual(report["prompt_bm_rows"], 2 * len(self.layer_ids))
                self.assertEqual(report["decode_bm_rows"], len(self.layer_ids))
                self.assertEqual(self.runtime.written_tokens(1), 1)
                self.runtime.detach_row(binding)
                old = binding

    def test_capture_requires_every_layer_fetch_even_with_zero_valid_rows(self):
        batch = prefill_batch((1,), (0,), (0,))
        batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
        batch.seq_lens = torch.tensor([0])
        self.runtime.begin_forward([], capture=True)
        k = torch.ones((1, 2), dtype=torch.bfloat16)
        for layer in self.layer_ids:
            self.runtime.write_layer(layer, k, k, batch)
        positions = torch.tensor([[0]])
        for layer in self.layer_ids:
            output = torch.full((1, 1, 1, 4), -7, dtype=torch.bfloat16)
            valid = self.runtime.selected_kv_valid(
                layer, batch.req_pool_indices, positions
            )
            self.runtime.fetch_selected_kv(
                layer, batch.req_pool_indices, positions, valid, output
            )
            self.assertTrue((output == -7).all())
            if layer == self.layer_ids[0]:
                with self.assertRaisesRegex(
                    RuntimeError, "missing local layer fetches"
                ):
                    self.runtime.end_forward()
        self.runtime.end_forward()
        self.assertEqual(self.copy_counts, [0] * (2 * len(self.layer_ids)))
        self.assertEqual(self.runtime.poll_completed(), [])


class TestGLM52KVFetchLayers(TestKVFetchLayers):
    """Shared selections still fetch distinct KV and complete all 78 layers."""

    layer_ids = tuple(range(78))


if __name__ == "__main__":
    unittest.main()
