"""Adapt logical rows to page-first storage with the deployed 64-bit UniDexCopy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from torch import Tensor

    from .manager import MempoolKVView


def _extent(
    view: MempoolKVView | None, tensor: Tensor, row_bytes: int
) -> tuple[int, int | None]:
    if view is None:
        return tensor.numel() * tensor.element_size() // row_bytes, None
    layout = view.layout
    layer_offset = view.layer * layout.page_size * layout.row_bytes
    return (layout.total_bytes - layer_offset) // row_bytes, view.device_base


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
            source_view.map_rows(source_rows // chunks) * chunks + source_rows % chunks
        )
    if target_view is not None:
        chunks = target_view.layout.row_bytes // row_bytes
        target_rows = (
            target_view.map_rows(target_rows // chunks) * chunks + target_rows % chunks
        )
    src_count, src_ptr = _extent(source_view, source, row_bytes)
    dst_count, dst_ptr = _extent(target_view, target, row_bytes)
    mask = (
        valid
        & (source_rows >= 0)
        & (source_rows < src_count)
        & (target_rows >= 0)
        & (target_rows < dst_count)
    )
    kernel(
        source,
        target,
        torch.where(mask, source_rows, 0),
        torch.where(mask, target_rows, 0),
        mask,
        src_count,
        dst_count,
        row_bytes,
        source_rows.numel(),
        block_dim,
        src_ptr,
        dst_ptr,
    )
