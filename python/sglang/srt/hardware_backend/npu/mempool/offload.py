"""Write temporary compact KV to local BM with explicit device metadata."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .layout import UINT32_MAX, positive_int
from .manager import MempoolKVView

if TYPE_CHECKING:
    from torch import Tensor


class MempoolKVOffload:
    """Write [rows, heads, dim] BF16 KV to the owning rank's logical layer."""

    def __init__(
        self,
        target: MempoolKVView,
        *,
        block_dim: int = 48,
        kernel: Any = None,
        feature_part: tuple[int, int] = (0, 1),
    ) -> None:
        """Retain the mapped destination; an injected kernel supports CPU tests."""
        import torch

        positive_int("block_dim", block_dim)
        if target.rank != target.owner.rank:
            raise ValueError("KV offload must target the owning rank's view")
        self.target = target
        self.block_dim = block_dim
        self._kernel = kernel
        self.part, self.parts = feature_part
        if target.layout.dim % self.parts or not 0 <= self.part < self.parts:
            raise ValueError(
                "feature writers must evenly partition the compact dimension"
            )
        # A CPU dtype placeholder preserves NPU dispatch from the source/indices.
        self._target_dtype = torch.empty(1, dtype=torch.bfloat16, device="cpu")

    def write(
        self,
        values: Tensor,
        *,
        slots: Tensor,
        positions: Tensor,
        valid: Tensor,
    ) -> None:
        """Enqueue masked writes without host tensor reads or device synchronization.

        Bounds masking also excludes unbound/padded rows. Request admission must
        reject capacity overflow separately; masking is not a success signal.
        The caller orders producer/consumer streams and retains the pool through
        graph use, then drains every access before release.
        """
        import torch

        layout = self.target.layout
        rows = slots.numel()
        if rows <= 0 or rows * layout.row_bytes > UINT32_MAX:
            raise ValueError("invalid source extent for UniDexCopy")
        if (
            positions.shape != slots.shape
            or valid.shape != slots.shape
            or slots.ndim != 1
        ):
            raise ValueError("write metadata must be equal-length vectors")
        if (
            slots.dtype != torch.int64
            or positions.dtype != torch.int64
            or valid.dtype != torch.bool
        ):
            raise ValueError(
                "write metadata requires int64 slots/positions and bool valid"
            )
        if positions.device != slots.device or valid.device != slots.device:
            raise ValueError("write metadata must share one device")
        if values.dtype != torch.bfloat16:
            raise ValueError("KV offload requires bfloat16")
        if tuple(values.shape) != (rows, layout.heads, layout.dim):
            raise ValueError("KV offload source must have shape [rows, heads, dim]")
        if not values.is_contiguous() or values.device != slots.device:
            raise ValueError(
                "KV offload requires contiguous source and inputs on one device"
            )
        if self._kernel is None:
            if values.device.type != "npu":
                raise ValueError("KV offload requires an NPU source")
            importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
            self._kernel = torch.ops.npu.unidex_copy

        valid = (
            valid
            & (slots >= 0)
            & (slots < layout.slots)
            & (positions >= 0)
            & (positions < layout.tokens)
        )
        destination = torch.where(valid, slots * layout.tokens + positions, 0)
        heads = torch.arange(layout.heads, dtype=torch.int64, device=values.device)
        source = torch.arange(rows, dtype=torch.int64, device=values.device)
        if self.parts > 1:
            source = (
                (source[:, None] * layout.heads + heads) * self.parts + self.part
            ).flatten()
            destination = (
                (destination[:, None] * layout.heads + heads) * self.parts + self.part
            ).flatten()
            valid = valid[:, None].expand(-1, layout.heads).flatten()
        chunks = layout.heads * self.parts if self.parts > 1 else 1
        # Always launch: a zero-valid capture must still contain the write path.
        self._kernel(
            values,
            self._target_dtype,
            source,
            destination,
            valid,
            rows * chunks,
            layout.slots * layout.tokens * chunks,
            layout.row_bytes // chunks,
            source.numel(),
            self.block_dim,
            None,
            self.target.device_base,
        )
