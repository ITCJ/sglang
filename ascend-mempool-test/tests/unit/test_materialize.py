"""Run real cache materialization with CPU implementations of NPU operations."""

import importlib
import sys
import unittest
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import test_fetch
import torch
from test_runtime import prefill_batch

from ascend_mempool.runtime import KVWriteExpectation
from ascend_sparse.config import SparseKVOffloadMode
from ascend_sparse.fixture import allocate_cache


class CPUStream:
    def __init__(self, *args):
        pass

    def record_event(self, event):
        event.ready = True

    def wait_event(self, event):
        if not event.ready:
            raise AssertionError("consumer ran before its producer event")


class CPUEvent:
    def __init__(self):
        self.ready = False


def cpu_copy(src, dst, si, di, valid, src_dims, dst_dims, **kwargs):
    width = src.shape[-1]
    dst.reshape(-1, width)[di[valid]] = src.reshape(-1, width)[si[valid]]


def cpu_lookup(table, rows, positions):
    if ((positions < -1) | (positions >= table.shape[1])).any():
        raise AssertionError("unsafe lookup indices")
    safe = positions.clamp(min=0).long()
    slots = table[rows.long()[:, None], safe]
    return (positions >= 0) & (slots >= 0), slots.clamp(min=0)


class TestMaterialize(unittest.TestCase):
    def setUp(self):
        # Import the production module, replacing only the unavailable NPU SDK.
        kernel = ModuleType("sgl_kernel_npu.sparsity_driven_kv_offload")
        kernel.create_shm_tensor = None
        kernel.slot_map_lookup = cpu_lookup
        kernel.unidex_copy_inplace = cpu_copy
        sdk = patch.dict(sys.modules, {kernel.__name__: kernel})
        sdk.start()
        self.addCleanup(sdk.stop)
        self.module = importlib.import_module("ascend_sparse.manager")
        npu = SimpleNamespace(
            stream=lambda stream: nullcontext(),
            current_stream=CPUStream,
            Stream=CPUStream,
            Event=CPUEvent,
        )
        patcher = patch.object(torch, "npu", npu, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        helper = test_fetch.TestKVFetch()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper
        # No host buffers exist: any implicit host fallback fails the test.
        self.cache = allocate_cache(
            rows=9,
            context=24,
            topk=4,
            layers=1,
            heads=1,
            dim=4,
            device="cpu",
            start_layer=5,
        )

    def test_bm_misses_refill_cache_and_become_hits_on_next_forward(self):
        h = self.helper
        h.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        h.p[2, 0] = 101
        h.p[2, 3] = 103
        positions = torch.tensor([[0, 3, 4, -1], [0, 3, 4, -1]])
        for offset in range(2):
            batch = h.start(offset=offset)
            output = torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16)
            valid = self.cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                positions,
                output,
                CPUStream(),
                mempool_runtime=h.runtime,
            )
            self.assertEqual(output[0, :, 0, 0].tolist(), [101, 103, 201, 0])
            self.assertTrue((output[1] == 0).all())
            self.assertEqual(valid.tolist(), [[True, True, True, False], [False] * 4])
            h.runtime.end_forward()
            h.events[-1].done = True
            h.runtime.poll_completed()
            # Poison the BM source after refill; the next call must use hits.
            h.p[2] = -99
            h.d[3, 0] = -88
        self.assertEqual(h.copy_counts, [2, 1, 0, 0])

    def test_attention_consumes_bm_values_with_short_topk_multiple_rows_and_padding(
        self,
    ):
        captured = []
        npu = ModuleType("torch_npu")

        def attention(query, key, value, indices, scaling, **kwargs):
            captured.append(
                (
                    key.clone(),
                    kwargs["key_rope"].clone(),
                    indices.clone(),
                    kwargs["actual_seq_lengths_kv"].clone(),
                )
            )
            return torch.zeros_like(query)

        npu.npu_sparse_flash_attention = attention
        with patch.dict(sys.modules, {"torch_npu": npu}):
            module = importlib.import_module("ascend_sparse.attention")
        h = self.helper
        h.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        h.runtime.bind(2, slot=5, prompt_tokens=2, prompt_slot=7)
        h.p[2, 0] = 101
        h.p[7, 0] = 107
        h.runtime.begin_forward(
            [KVWriteExpectation(1, 4, 1), KVWriteExpectation(2, 2, 1)]
        )
        batch = prefill_batch((1, 1, 1), (0, 0, 0), (1, 2, 0))
        batch.batch_size = 3
        batch.seq_lens = torch.tensor([5, 3, 0])
        batch.forward_mode = SimpleNamespace(
            is_decode=lambda: True, is_extend_without_speculative=lambda: False
        )
        k = torch.tensor([[201, 201], [205, 205], [0, 0]], dtype=torch.bfloat16)
        h.runtime.write_layer(5, k, k, batch)
        backend = SimpleNamespace(
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            device="cpu",
            sparse_kv_manager=self.cache,
            mempool_runtime=h.runtime,
            forward_metadata=SimpleNamespace(
                actual_seq_lengths_q=torch.ones(3), actual_seq_lengths_kv=batch.seq_lens
            ),
        )
        layer = SimpleNamespace(
            layer_id=5, tp_k_head_num=1, tp_q_head_num=1, scaling=1.0
        )
        module.forward_sparsity_driven_kv_offload(
            backend,
            k,
            k,
            k,
            layer,
            batch,
            save_kv_cache=False,
            q_rope=k,
            k_rope=k,
            topk_indices=torch.tensor([[0, 4], [0, 2], [0, 2]]),
        )
        h.runtime.end_forward()
        h.events[-1].done = True
        h.runtime.poll_completed()
        key, rope, indices, lengths = captured[0]
        self.assertEqual(
            key[:, :, 0, 0].tolist(), [[101, 201, 0, 0], [107, 205, 0, 0], [0, 0, 0, 0]]
        )
        self.assertTrue(torch.equal(key, rope))
        self.assertEqual(
            indices[:, 0, 0].tolist(), [[0, 1, -1, -1], [0, 1, -1, -1], [0, -1, -1, -1]]
        )
        self.assertEqual(lengths.tolist(), [2, 2, 1])

    def test_fixed_npu_lookup_width_with_small_context_and_padding(self):
        self.cache = allocate_cache(
            rows=9,
            context=24,
            topk=2048,
            layers=1,
            heads=1,
            dim=4,
            device="cpu",
            start_layer=5,
        )

        def fixed_lookup(table, rows, positions):
            self.assertEqual(positions.shape[1], 2048)
            self.assertEqual(table.shape[1] % 8, 0)
            return cpu_lookup(table, rows, positions)

        h = self.helper
        h.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        h.p[2, 0] = 101
        batch = h.start()
        positions = torch.full((2, 2048), -1, dtype=torch.int64)
        positions[:, :2] = torch.tensor([0, 4])
        output = torch.zeros((2, 2048, 1, 4), dtype=torch.bfloat16)
        with patch.object(self.module, "slot_map_lookup", side_effect=fixed_lookup):
            valid = self.cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                positions,
                output,
                CPUStream(),
                mempool_runtime=h.runtime,
            )
        self.assertEqual(output[0, :2, 0, 0].tolist(), [101, 201])
        self.assertTrue((output[0, 2:] == 0).all())
        self.assertTrue((output[1] == 0).all())
        self.assertEqual(valid.sum(dim=1).tolist(), [2, 0])
        self.assertEqual(h.copy_counts, [1, 1])
        h.runtime.end_forward()
        h.events[-1].done = True
        h.runtime.poll_completed()

    def test_unwritten_cache_hit_cannot_be_copied_or_refilled(self):
        h = self.helper
        binding = h.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        # Even a stale hit entry must obey the current request's written range.
        self.cache.device_slot_map[0][1, 5] = 0
        self.cache.device_kv_buffer[0][1, 0] = 99
        batch = h.start(rows=(1, 2, 0))  # Row 2 is unbound; row 0 is padding.
        output = torch.zeros((3, 4, 1, 4), dtype=torch.bfloat16)
        valid = self.cache.materialize_selected_kv(
            SimpleNamespace(layer_id=5),
            batch,
            torch.tensor([[5, 99, -1, -1], [0, 1, -1, -1], [0, 1, -1, -1]]),
            output,
            CPUStream(),
            mempool_runtime=h.runtime,
        )
        self.assertFalse(valid.any())
        self.assertTrue((output == 0).all())
        self.assertTrue((self.cache.device_slot_map[0] == -1).all())
        self.assertEqual(self.cache.device_kv_buffer[0][1, 0, 0, 0].item(), 99)
        h.runtime.end_forward()
        h.events[-1].done = True
        with self.assertRaisesRegex(RuntimeError, r"row=1 position=5 invalid_kv=2"):
            h.runtime.poll_completed()
        with self.assertRaisesRegex(RuntimeError, "fault"):
            h.runtime.detach_row(binding)

    def test_reset_removes_old_hits_before_row_and_slot_reuse(self):
        h = self.helper
        for step, slot in enumerate((2, 7)):
            binding = h.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=slot)
            h.p[slot, 0] = 101 + step
            batch = h.start()
            output = torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16)
            self.cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                torch.tensor([[0, -1, -1, -1], [0, -1, -1, -1]]),
                output,
                CPUStream(),
                mempool_runtime=h.runtime,
            )
            self.assertEqual(output[0, 0, 0, 0].item(), 101 + step)
            h.runtime.end_forward()
            h.events[-1].done = True
            h.runtime.poll_completed()
            h.runtime.detach_row(binding)
            self.cache.reset_requests([1])
        self.assertEqual(h.copy_counts, [1, 0, 1, 0])

    def test_ordinary_mode_keeps_host_copy_and_cache_refill(self):
        self.cache.mode = SparseKVOffloadMode.PD_DECODE_OFFLOAD
        self.cache.host_kv_buffer = [
            torch.full((9, 24, 1, 4), -3, dtype=torch.bfloat16)
        ]
        self.cache.dev_ptr_list = [0]
        self.cache.host_kv_buffer[0][1, 2] = 53
        batch = SimpleNamespace(
            req_pool_indices=torch.tensor([1, 0]), seq_lens=torch.tensor([5, 0])
        )
        for _ in range(2):
            output = torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16)
            self.cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                torch.tensor([[2, -1, -1, -1], [2, -1, -1, -1]]),
                output,
                CPUStream(),
            )
            self.assertEqual(output[0, 0, 0, 0].item(), 53)
            self.assertTrue((output[1] == 0).all())
            self.cache.host_kv_buffer[0].fill_(-99)

    def test_formal_materialization_never_falls_back_without_runtime(self):
        with self.assertRaisesRegex(RuntimeError, "runtime"):
            self.cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                SimpleNamespace(
                    req_pool_indices=torch.tensor([1]), seq_lens=torch.tensor([5])
                ),
                torch.tensor([[0, -1, -1, -1]]),
                torch.zeros((1, 4, 1, 4), dtype=torch.bfloat16),
                CPUStream(),
            )


if __name__ == "__main__":
    unittest.main()
