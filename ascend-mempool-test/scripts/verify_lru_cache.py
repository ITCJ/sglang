"""Ticket27 component gate: real manager/kernels, synthetic KV, no BM or model."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class SelectedSources:
    """HBM fixtures stand in for the three backing sources; cache is real."""

    observer = None

    def __init__(self, cache: Any, output: Any, torch: Any):
        if TYPE_CHECKING:
            from sglang.srt.hardware_backend.npu.mempool.multibase import (
                BaseTable,
                MultibaseCopy,
            )
        else:
            from ascend_mempool.multibase import BaseTable, MultibaseCopy

        self.torch = torch
        tokens = torch.arange(8192, device=cache.device)
        self.values = (
            ((tokens % 127) + 1)
            .to(torch.bfloat16)[:, None, None]
            .expand(-1, 1, 576)
            .contiguous()
        )
        buffers = [
            cache.device_kv_buffer[0],
            self.values[:4096],
            self.values[4096:8191],
            self.values[8191:],
        ]
        self.cache_rows = cache.size * cache.device_cache_capacity
        self.copy = MultibaseCopy(
            BaseTable(
                [(b.data_ptr(), b.numel() // 576) for b in buffers],
                cache.device,
                buffers,
            ),
            BaseTable(
                [(output.data_ptr(), output.numel() // 576)], cache.device, [output]
            ),
            output.numel() // 576,
            row_bytes=1152,
        )

    def selected_kv_valid(self, layer: int, rows: Any, topk: Any) -> Any:
        return (rows[:, None] > 0) & (topk >= 0) & (topk < 8192) & (topk != 7000)

    def fetch_selected_kv(
        self,
        layer: int,
        rows: Any,
        topk: Any,
        miss: Any,
        output: Any,
        *,
        cache_buffer: Any,
        cache_rows: Any,
        cache_valid: Any,
    ) -> None:
        t = self.torch
        source = t.where(
            cache_valid, cache_rows, self.cache_rows + topk.long().flatten()
        )
        self.copy.submit(
            source,
            t.arange(topk.numel(), device=topk.device),
            cache_valid | miss.flatten(),
        )


def verify(device_id: int) -> list[dict[str, Any]]:
    import torch

    importlib.import_module("torch_npu")
    from sglang.srt.hardware_backend.npu.memory_pool_npu import NPUMLATokenToKVPool
    from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
        SparseKVOffloadMode,
    )
    from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.manager import (
        SparseKVCacheManager,
    )
    from sglang.srt.managers.schedule_batch import ReqKvInfo
    from sglang.srt.mem_cache.memory_pool import ReqToTokenPool

    torch.npu.set_device(device_id)
    device = f"npu:{device_id}"
    mode = SparseKVOffloadMode.PD_DECODE_MEMPOOL
    pool = NPUMLATokenToKVPool(
        size=128,
        page_size=128,
        dtype=torch.bfloat16,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
        layer_num=1,
        device=device,
        enable_memory_saver=False,
        index_head_dim=128,
        sparse_kv_offload_mode=mode,
    )
    rows = ReqToTokenPool(
        2, max_context_len=8192, device=device, enable_memory_saver=False
    )
    cache = SparseKVCacheManager(
        rows, SimpleNamespace(get_kvcache=lambda: pool), 2048, mode=mode
    )
    req = SimpleNamespace(kv=ReqKvInfo())
    row = rows.alloc([req])[0]
    capacity = cache.device_cache_capacity
    assert capacity == 4096
    stream = torch.npu.current_stream()
    results = []
    for batch_size, graph_mode in ((1, False), (16, True)):
        output = torch.full(
            (batch_size, 2048, 1, 576), -9, dtype=torch.bfloat16, device=device
        )
        topk = torch.full((batch_size, 2048), -1, dtype=torch.int32, device=device)
        batch = SimpleNamespace(
            req_pool_indices=torch.zeros(batch_size, dtype=torch.int32, device=device),
            seq_lens=torch.zeros(batch_size, dtype=torch.int32, device=device),
        )
        batch.req_pool_indices[0], batch.seq_lens[0] = row, 8192
        source = SelectedSources(cache, output, torch)

        def seed() -> None:
            cache.reset_requests([row])
            cache.device_slot_map[0][row, :capacity] = torch.arange(
                capacity, dtype=torch.int32, device=device
            )
            cache.device_slot_tokens[0][row] = torch.arange(
                capacity, dtype=torch.int32, device=device
            )
            cache.device_kv_buffer[0][row].copy_(source.values[:capacity])
            topk.fill_(-1)
            topk[0, :6] = torch.tensor(
                [0, 4096, 8191, 7000, -2, 8192], dtype=torch.int32, device=device
            )

        def step() -> None:
            output.fill_(-9)
            cache.materialize_selected_kv(
                SimpleNamespace(layer_id=0),
                batch,
                topk,
                output,
                stream,
                mempool_runtime=source,
            )
            for event in (
                cache.hit_done,
                cache.miss_done,
                cache.refill_done,
                cache.slot_map_done,
            ):
                stream.wait_event(event)

        seed()
        step()  # Warm the same call path, outside Graph capture.
        torch.npu.synchronize()
        graph = None
        if graph_mode:
            seed()
            torch.npu.synchronize()
            graph = torch.npu.NPUGraph()
            with torch.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
                step()
        seed()
        torch.npu.synchronize()
        graph.replay() if graph is not None else step()
        torch.npu.synchronize()
        expected = torch.full_like(output.cpu(), -9)
        expected[0, :3].copy_(source.values[[0, 4096, 8191]].cpu())
        torch.testing.assert_close(output.cpu(), expected, rtol=0, atol=0)
        mapping = cache.device_slot_map[0][row].cpu()
        assert mapping[0] == 0 and mapping[4096] == 1 and mapping[8191] == 2
        assert mapping[1] == -1 and mapping[2] == -1 and mapping[7000] == -1
        order = cache.device_lru_slots[0][row].cpu().tolist()
        stamps = dict(zip(order, cache.device_lru_slot_stamps[0][row].cpu().tolist()))
        assert (stamps[0], stamps[1], stamps[2]) == (0, 4, 4)
        assert bool((cache.device_slot_tokens[0][0].cpu() == -1).all())
        # New probationary entry is evicted before untouched resident entries;
        # the just-hit probationary entry is promoted and keeps its physical slot.
        topk.fill_(-1)
        topk[0, :2] = torch.tensor([4096, 4097], dtype=torch.int32, device=device)
        graph.replay() if graph is not None else step()
        torch.npu.synchronize()
        mapping = cache.device_slot_map[0][row].cpu()
        assert mapping[4096] == 1 and mapping[4097] == 2 and mapping[8191] == -1
        assert mapping[3] == 3
        torch.testing.assert_close(
            output[0, :2].cpu(), source.values[[4096, 4097]].cpu(), rtol=0, atol=0
        )
        rows.free(req)
        req = SimpleNamespace(kv=ReqKvInfo())
        assert rows.alloc([req]) == [row]
        assert bool((cache.device_slot_tokens[0][row].cpu() == -1).all())
        assert bool((cache.device_lru_slot_stamps[0][row].cpu() == 0).all())
        # Replay with reused storage must miss and fetch fresh backing values.
        source.values[4096].fill_(77)
        graph.replay() if graph is not None else step()
        torch.npu.synchronize()
        assert bool((output[0, 0].cpu() == 77).all())
        results.append(
            dict(
                batch=batch_size,
                graph=graph_mode,
                eviction=True,
                probation=True,
                reuse=True,
                kv_equal=True,
            )
        )
    rows.free(req)
    rows.clear()
    assert bool((cache.device_slot_tokens[0].cpu() == -1).all())
    torch.npu.synchronize()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("run without Python -O / PYTHONOPTIMIZE")
    os.environ.update(
        SGLANG_NPU_SPARSE_KV_ENABLE_LRU="1",
        SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR="2",
        SGLANG_NPU_SPARSE_KV_PROBATION_AGE="4",
    )
    report: dict[str, Any] = dict(
        status="failed",
        coverage="component: synthetic HBM backing sources; real manager, multibase and LRU; no BM/service",
    )
    try:
        report["cases"] = verify(args.device_id)
        report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
