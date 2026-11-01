"""Adapt logical rows to page-first storage within UniDexCopy's 32-bit windows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .layout import UINT32_MAX

if TYPE_CHECKING:
    from torch import Tensor

    from .manager import MempoolKVView


def _windows(
    view: MempoolKVView | None, tensor: Tensor, row_bytes: int
) -> list[tuple[int, int, int | None]]:
    if view is None:
        return [(0, tensor.numel() * tensor.element_size() // row_bytes, None)]
    layout = view.layout
    layer_offset = view.layer * layout.page_size * layout.row_bytes
    if layout.total_bytes <= UINT32_MAX:
        return [(0, (layout.total_bytes - layer_offset) // row_bytes, view.device_base)]
    # Normally one window per slot. Large component pools can have slots above
    # 4 GiB; split those only at whole-page boundaries, preserving the same API.
    window_bytes = min(
        layout.slot_bytes, UINT32_MAX // layout.page_bytes * layout.page_bytes
    )
    return [
        (
            offset // row_bytes,
            (min(window_bytes, layout.total_bytes - offset) - layer_offset)
            // row_bytes,
            view.device_base + offset,
        )
        for offset in range(0, layout.total_bytes, window_bytes)
    ]


def copy_rows(
    kernel: Any,
    source: Tensor,
    target: Tensor,
    source_rows: Tensor,
    target_rows: Tensor,
    valid: Tensor,
    *,
    row_bytes: int,
    block_dim: int = 48,
    source_view: MempoolKVView | None = None,
    target_view: MempoolKVView | None = None,
) -> None:
    """Submit logical row copies; native tensors retain their existing layout.

    Smaller feature rows use the same geometry by retaining the in-token part.
    No tensor value is read by the host, including on zero-valid graph warmup.
    """
    import torch

    if source_view is not None:
        chunks = source_view.layout.row_bytes // row_bytes
        source_rows = (
            source_view.layout.physical_rows(source_rows // chunks) * chunks
            + source_rows % chunks
        )
    if target_view is not None:
        chunks = target_view.layout.row_bytes // row_bytes
        target_rows = (
            target_view.layout.physical_rows(target_rows // chunks) * chunks
            + target_rows % chunks
        )
    for src_start, src_count, src_ptr in _windows(source_view, source, row_bytes):
        for dst_start, dst_count, dst_ptr in _windows(target_view, target, row_bytes):
            mask = (
                valid
                & (source_rows >= src_start)
                & (source_rows < src_start + src_count)
                & (target_rows >= dst_start)
                & (target_rows < dst_start + dst_count)
            )
            kernel(
                source,
                target,
                torch.where(mask, source_rows - src_start, 0),
                torch.where(mask, target_rows - dst_start, 0),
                mask,
                src_count,
                dst_count,
                row_bytes,
                source_rows.numel(),
                block_dim,
                src_ptr,
                dst_ptr,
            )
