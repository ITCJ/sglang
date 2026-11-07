"""Allocate only the cache resources used by standalone materialization checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


def allocate_cache(
    *,
    rows: int,
    context: int,
    topk: int,
    layers: int,
    heads: int,
    dim: int,
    device: str,
    start_layer: int = 0,
) -> Any:
    """Exercise the real manager without allocating SGLang native/host pools.

    This fixture deliberately starts at the materialization seam. It does not
    verify production allocation, PD admission, or full service startup.
    """
    import torch

    if TYPE_CHECKING:
        from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
            SparseKVOffloadMode,
        )
        from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.manager import (
            SparseKVCacheManager,
        )
    else:
        from .config import SparseKVOffloadMode
        from .manager import SparseKVCacheManager

    cache = SparseKVCacheManager.__new__(SparseKVCacheManager)
    cache.mode = SparseKVOffloadMode.PD_DECODE_MEMPOOL
    cache.size, cache.max_context_len, cache.sparse_context_len = rows, context, topk
    cache.start_layer, cache.layer_num, cache.device = start_layer, layers, device
    cache.device_kv_buffer = [
        torch.full((rows, topk, heads, dim), -9, dtype=torch.bfloat16, device=device)
        for _ in range(layers)
    ]
    cache._slot_map_width = (context // 8 + 1) * 8
    cache.device_slot_map = [
        torch.full(
            (rows + 1, cache._slot_map_width), -1, dtype=torch.int32, device=device
        )
        for _ in range(layers)
    ]
    cache._device_slot_map_minus_one = torch.full_like(cache.device_slot_map[0], -1)
    cache._device_cache_slot_ids = torch.arange(topk, device=device)
    for name in ("d2d_hit", "h2d_miss", "refill", "slot_map"):
        setattr(cache, f"_materialize_{name}_stream", torch.npu.Stream())
    for name in ("hit", "miss", "refill", "slot_map"):
        setattr(cache, f"{name}_done", torch.npu.Event())
    return cache
