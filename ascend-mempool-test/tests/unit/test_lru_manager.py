"""Real manager behavior with CPU doubles only at the external NPU boundary."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_sparse_resources
import torch
from test_lru_budget import load_env
from test_materialize import CPUStream, cpu_copy

from ascend_sparse.config import SparseKVOffloadMode as Mode
from ascend_sparse.config import get_sparsity_driven_kv_offload_fixed_memory_size


def lookup(table, rows, positions, pos_mask_size=None):
    slots = table[rows.long()[:, None], positions.clamp(min=0).long()]
    hit = (positions >= 0) & (slots >= 0)
    slots = torch.where(hit, slots, -1).int()
    if pos_mask_size is None:
        return hit, slots
    mask = torch.zeros((rows.numel(), pos_mask_size), dtype=torch.int32)
    for b in range(rows.numel()):
        mask[b, slots[b, hit[b]].long()] = 1
    return hit, slots, mask


def update(
    rows, topk, positions, hits, order, stamps, *, max_context_len, probation_age
):
    victims = torch.full_like(topk, -1)
    counts = torch.zeros(rows.numel(), dtype=torch.int32)
    for b, row in enumerate(rows.tolist()):
        if row >= order.shape[0]:
            continue
        old = list(zip(order[row].tolist(), stamps[row].tolist()))
        misses = (
            ((topk[b] >= 0) & (topk[b] < max_context_len) & (positions[b] < 0))
            .nonzero()
            .flatten()
        )
        available = [slot for slot, _ in old if not hits[b, slot]]
        chosen = available[: misses.numel()]
        victims[b, misses] = torch.tensor(chosen, dtype=torch.int32)
        counts[b] = len(chosen)
        ages = [
            (slot, 0 if hits[b, slot] else probation_age if slot in chosen else age + 1)
            for slot, age in old
        ]
        ages.sort(key=lambda pair: -pair[1])
        order[row] = torch.tensor([slot for slot, _ in ages])
        stamps[row] = torch.tensor([age for _, age in ages])
    return victims, counts


def write(table, rows, topk, victims, counts, tokens, *, max_context_len):
    for b, row in enumerate(rows.tolist()):
        if row >= tokens.shape[0]:
            continue
        for pos in (victims[b] >= 0).nonzero().flatten().tolist():
            slot, token = int(victims[b, pos]), int(topk[b, pos])
            old = int(tokens[row, slot])
            if old >= 0:
                table[row, old] = -1
            tokens[row, slot], table[row, token] = token, slot


class TestLRUManager(unittest.TestCase):
    def test_lru_keeps_ordinary_host_offload_roundtrip(self):
        fixture = test_sparse_resources.TestSparseResources()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.native.kv_lora_rank, fixture.native.qk_rope_head_dim = 512, 64
        sdk = sys.modules["sgl_kernel_npu.sparsity_driven_kv_offload"]
        sdk.fused_timestamp_lru_metadata_update_with_probation = update
        sdk.parallel_lru_metadata_write = write
        with (
            patch.dict(
                os.environ,
                SGLANG_NPU_SPARSE_KV_ENABLE_LRU="1",
                SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR="2",
            ),
            patch.object(fixture.module, "slot_map_lookup", lookup),
        ):
            cache = fixture.module.SparseKVCacheManager(
                fixture.rows, fixture.allocator, 2048, mode=Mode.LOCAL_OFFLOAD
            )
            from verify_resources import check_host_roundtrip

            check_host_roundtrip(
                cache,
                SimpleNamespace(kv=SimpleNamespace(req_pool_idx=1)),
                torch.tensor([7]),
                torch,
            )

    def test_capacity_hit_miss_padding_and_request_reuse(self):
        fixture = test_sparse_resources.TestSparseResources()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.rows.max_context_len = 8192
        sdk = sys.modules["sgl_kernel_npu.sparsity_driven_kv_offload"]
        sdk.fused_timestamp_lru_metadata_update_with_probation = update
        sdk.parallel_lru_metadata_write = write
        with (
            patch.dict(sys.modules, {"sglang.srt.environ": load_env()}),
            patch.dict(
                os.environ,
                {
                    "SGLANG_NPU_SPARSE_KV_ENABLE_LRU": "1",
                    "SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR": "2",
                },
            ),
            patch.object(fixture.module, "slot_map_lookup", lookup),
        ):
            cache = fixture.module.SparseKVCacheManager(
                fixture.rows, fixture.allocator, 2048, mode=Mode.PD_DECODE_MEMPOOL
            )
            self.assertEqual(cache.device_cache_capacity, 4096)
            allocated = sum(
                t.nbytes
                for buffers in (
                    cache.device_kv_buffer,
                    cache.device_slot_map,
                    cache.device_slot_tokens,
                    cache.device_lru_slots,
                    cache.device_lru_slot_stamps,
                )
                for t in buffers
            )
            allocated += sum(
                t.nbytes
                for t in (
                    cache._initial_lru_slot_order,
                    cache._device_cache_slot_ids,
                    cache._device_slot_map_minus_one,
                )
            )
            self.assertEqual(
                allocated,
                get_sparsity_driven_kv_offload_fixed_memory_size(
                    mode=Mode.PD_DECODE_MEMPOOL,
                    request_rows=3,
                    max_context_len=8192,
                    sparse_context_len=2048,
                    device_cache_capacity=4096,
                    enable_lru=True,
                    kv_head_dim=4,
                    num_layers=2,
                    element_size=2,
                    page_size=4,
                ),
            )
            cache.device_slot_map[0][1, :4096] = torch.arange(4096)
            cache.device_slot_tokens[0][1] = torch.arange(4096)
            cache.device_kv_buffer[0][1].fill_(11)
            cache.device_kv_buffer[0][2].fill_(91)
            batch = SimpleNamespace(
                req_pool_indices=torch.tensor([1] + [0] * 15),
                seq_lens=torch.tensor([8000] + [0] * 15),
            )
            topk = torch.full((16, 2048), -1, dtype=torch.int32)
            topk[0, :3] = torch.tensor([0, 4096, 7000])
            selected = torch.full((16, 2048, 1, 4), -9, dtype=torch.bfloat16)

            def fetch(
                layer,
                rows,
                topk,
                miss,
                output,
                *,
                cache_buffer,
                cache_rows,
                cache_valid,
            ):
                cpu_copy(
                    cache_buffer,
                    output,
                    cache_rows,
                    torch.arange(topk.numel()),
                    cache_valid,
                    2,
                    2,
                )
                output[miss] = 23

            runtime = SimpleNamespace(
                observer=None,
                selected_kv_valid=lambda layer, rows, tokens: (
                    (rows[:, None] > 0) & (tokens >= 0) & (tokens < 7000)
                ),
                fetch_selected_kv=fetch,
            )
            cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                topk,
                selected,
                CPUStream(),
                mempool_runtime=runtime,
            )
            self.assertEqual(selected[0, :3, 0, 0].tolist(), [11, 23, -9])
            self.assertEqual(cache.device_slot_map[0][1, 0].item(), 0)
            self.assertEqual(cache.device_slot_map[0][1, 4096].item(), 1)
            self.assertEqual(cache.device_slot_map[0][1, 1].item(), -1)
            self.assertEqual(cache.device_slot_map[0][1, 7000].item(), -1)
            self.assertTrue((cache.device_kv_buffer[0][2] == 91).all())
            self.assertTrue((cache.device_slot_map[0][0] == -1).all())
            self.assertEqual(cache.device_kv_buffer[0][1, :2, 0, 0].tolist(), [11, 23])
            # Probation stamps distinguish the hit from a newly admitted token.
            stamps = dict(
                zip(
                    cache.device_lru_slots[0][1].tolist(),
                    cache.device_lru_slot_stamps[0][1].tolist(),
                )
            )
            self.assertEqual((stamps[0], stamps[1]), (0, 4))
            cache.reset_requests([1])
            self.assertTrue((cache.device_slot_map[0][1] == -1).all())
            self.assertTrue((cache.device_slot_tokens[0][1] == -1).all())
            self.assertTrue((cache.device_lru_slot_stamps[0][1] == 0).all())
            self.assertEqual(cache.device_lru_slots[0][1].tolist(), list(range(4096)))
            cache.device_lru_slot_stamps[0][1].fill_(42)
            fixture.rows.clear()
            self.assertTrue((cache.device_lru_slot_stamps[0] == 0).all())


if __name__ == "__main__":
    unittest.main()
