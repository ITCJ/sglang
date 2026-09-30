"""Write temporary compact KV to local BM using fixed graph input addresses."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Optional

from .layout import UINT32_MAX, positive_int
from .manager import MempoolKVView

if TYPE_CHECKING:
    from torch import Tensor


class MempoolWriteInputs:
    """Hold fixed device buffers; callers update slot/token/valid values in place."""

    def __init__(self, rows: int, device: str) -> None:
        """Start every row unbound so capture and warmup cannot touch request KV."""
        import torch

        positive_int("rows", rows)
        self.rows = rows
        self.slots = torch.full((rows,), -1, dtype=torch.int64, device=device)
        self.positions = torch.full((rows,), -1, dtype=torch.int64, device=device)
        self.valid = torch.zeros((rows,), dtype=torch.bool, device=device)


class MempoolKVOffload:
    """Write [rows, heads, dim] BF16 KV to the owning rank's logical layer."""

    def __init__(
        self,
        target: MempoolKVView,
        inputs: Optional[MempoolWriteInputs] = None,
        block_dim: int = 48,
        kernel: Any = None,
    ) -> None:
        """Prepare fixed copy metadata; an injected kernel supports CPU tests."""
        import torch

        positive_int("block_dim", block_dim)
        if target.rank != target.owner.rank:
            raise ValueError("KV offload must target the owning rank's view")
        if inputs is not None and inputs.rows * target.layout.row_bytes > UINT32_MAX:
            raise ValueError("source span exceeds UniDexCopy's UINT32_MAX limit")
        self.target = target
        self.inputs = inputs
        self.block_dim = block_dim
        self._kernel = kernel
        # A CPU dtype placeholder preserves NPU dispatch from the source/indices.
        self._target_dtype = torch.empty(1, dtype=torch.bfloat16, device="cpu")

    def write(
        self,
        values: Tensor,
        *,
        slots: Optional[Tensor] = None,
        positions: Optional[Tensor] = None,
        valid: Optional[Tensor] = None,
    ) -> None:
        """Enqueue masked writes without host tensor reads or device synchronization.

        Bounds masking also excludes unbound/padded rows. Request admission must
        reject capacity overflow separately; masking is not a success signal.
        The caller orders producer/consumer streams and retains the pool through
        graph use, then drains every access before release.
        """
        import torch

        layout = self.target.layout
        if slots is None and positions is None and valid is None:
            if self.inputs is None:
                raise ValueError("slots, positions and valid must be supplied together")
            slots, positions, valid = (
                self.inputs.slots,
                self.inputs.positions,
                self.inputs.valid,
            )
        if slots is None or positions is None or valid is None:
            raise ValueError("slots, positions and valid must be supplied together")
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
        # Always launch: a zero-valid capture must still contain the write path.
        self._kernel(
            values,
            self._target_dtype,
            torch.arange(rows, dtype=torch.int64, device=values.device),
            destination,
            valid,
            rows,
            layout.slots * layout.tokens,
            layout.row_bytes,
            rows,
            self.block_dim,
            None,
            self.target.device_base,
        )
