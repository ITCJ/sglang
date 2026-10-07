"""Build prompt/decode UniDexCopy indices using graph-updatable device inputs."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Optional

import msgspec

from .layout import UINT32_MAX, positive_int
from .manager import MempoolKVManager, MempoolKVView

if TYPE_CHECKING:
    from torch import Tensor


class CopyIndices(msgspec.Struct, frozen=True):
    """Describe one source's masked rows and their destination positions."""

    src_index: Tensor
    dst_index: Tensor
    valid: Tensor


class SparseCopyInputs:
    """Keep source bindings and logical positions at stable capture addresses."""

    def __init__(self, batch_rows: int, topk: int, device: str) -> None:
        """Allocate an initially inactive selection, including padded top-k columns."""
        import torch

        positive_int("batch_rows", batch_rows)
        positive_int("topk", topk)
        self.batch_rows = batch_rows
        self.topk = topk
        self.p_slots = torch.full((batch_rows,), -1, dtype=torch.int64, device=device)
        self.d_slots = torch.full_like(self.p_slots, -1)
        self.prompt_lengths = torch.zeros_like(self.p_slots)
        self.decode_lengths = torch.zeros_like(self.p_slots)
        self.positions = torch.full(
            (batch_rows, topk), -1, dtype=torch.int64, device=device
        )
        self.valid = torch.zeros_like(self.positions, dtype=torch.bool)
        self.active = torch.zeros((batch_rows,), dtype=torch.bool, device=device)
        self.dst_index = torch.arange(
            batch_rows * topk, dtype=torch.int64, device=device
        )


class SparseKVCopy:
    """Gather selected prompt and decode rows into one compact KV buffer."""

    def __init__(
        self,
        prompt: MempoolKVView,
        decode: MempoolKVView,
        inputs: SparseCopyInputs,
        block_dim: int = 48,
        kernel: Any = None,
        output: Optional[Tensor] = None,
    ) -> None:
        """Allow sequential layers to share scratch storage on the submission stream."""
        import torch

        positive_int("block_dim", block_dim)
        if prompt.owner is not decode.owner or prompt.layer != decode.layer:
            raise ValueError("copy sources must be corresponding layers of one pool")
        if prompt.rank != 0 or decode.rank != 1:
            raise ValueError("prompt/decode sources must use pool ranks 0/1")
        if inputs.batch_rows * inputs.topk * prompt.layout.row_bytes > UINT32_MAX:
            raise ValueError("destination span exceeds UniDexCopy's UINT32_MAX limit")
        self.prompt = prompt
        self.decode = decode
        self.inputs = inputs
        self.block_dim = block_dim
        self._kernel = kernel
        shape = (inputs.batch_rows, inputs.topk, prompt.layout.heads, prompt.layout.dim)
        if output is None:
            output = torch.empty(
                shape, dtype=torch.bfloat16, device=inputs.positions.device
            )
        if (
            tuple(output.shape) != shape
            or output.dtype != torch.bfloat16
            or output.device != inputs.positions.device
            or not output.is_contiguous()
        ):
            raise ValueError(
                "copy output must be contiguous BF16 storage matching the selection"
            )
        self.output = output
        # Raw-pointer mode checks dtype, not source storage size. A meta tensor
        # would select Meta dispatch instead of the registered NPU kernel.
        self._source_dtype = torch.empty(1, dtype=torch.bfloat16, device="cpu")

    def routes(self) -> tuple[CopyIndices, CopyIndices]:
        """Mask unwritten positions and translate each role's independent slot."""
        import torch

        inputs = self.inputs
        p_layout, d_layout = self.prompt.layout, self.decode.layout
        binding_valid = (
            inputs.active
            & (inputs.p_slots >= 0)
            & (inputs.p_slots < p_layout.slots)
            & (inputs.d_slots >= 0)
            & (inputs.d_slots < d_layout.slots)
            & (inputs.prompt_lengths >= 0)
            & (inputs.prompt_lengths <= p_layout.tokens)
            & (inputs.decode_lengths >= 0)
            & (inputs.decode_lengths <= d_layout.tokens)
        ).unsqueeze(1)
        position = inputs.positions
        prompt_length = inputs.prompt_lengths.unsqueeze(1)
        decode_position = position - prompt_length
        p_valid = (
            binding_valid & inputs.valid & (position >= 0) & (position < prompt_length)
        )
        d_valid = (
            binding_valid
            & inputs.valid
            & (decode_position >= 0)
            & (decode_position < inputs.decode_lengths.unsqueeze(1))
        )
        p_index = torch.where(
            p_valid, inputs.p_slots.unsqueeze(1) * p_layout.tokens + position, 0
        )
        d_index = torch.where(
            d_valid,
            inputs.d_slots.unsqueeze(1) * d_layout.tokens + decode_position,
            0,
        )
        return (
            CopyIndices(p_index.flatten(), inputs.dst_index, p_valid.flatten()),
            CopyIndices(d_index.flatten(), inputs.dst_index, d_valid.flatten()),
        )

    def gather(self) -> Tensor:
        """Enqueue both sources, including zero-valid sources, without host reads."""
        import torch

        if self._kernel is None:
            importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
            self._kernel = torch.ops.npu.unidex_copy
        routes = self.routes()
        for source, indices in zip((self.prompt, self.decode), routes):
            self._kernel(
                self._source_dtype,
                self.output,
                indices.src_index,
                indices.dst_index,
                indices.valid,
                source.layout.slots * source.layout.tokens,
                self.inputs.batch_rows * self.inputs.topk,
                source.layout.row_bytes,
                indices.src_index.numel(),
                self.block_dim,
                source.device_base,
                None,
            )
        return self.output


class KVFetch:
    """Fetch cache misses directly into the caller's selected KV."""

    def __init__(
        self, manager: MempoolKVManager, block_dim: int, kernel: Any = None
    ) -> None:
        self.manager = manager
        self.block_dim = block_dim
        self.kernel = kernel
        self._copies: dict[tuple[int, int, int], SparseKVCopy] = {}

    def gather(
        self,
        layer: int,
        req_rows: Tensor,
        positions: Tensor,
        misses: Tensor,
        output: Tensor,
        row_prompt_slot: Tensor,
        row_decode_slot: Tensor,
        row_prompt_len: Tensor,
        row_decode_len: Tensor,
    ) -> None:
        """Enqueue both copies on the miss stream, including zero-valid sources.

        The caller orders binding/current-layer writes before this stream, and
        joins it before attention/refill or the next use of these fixed inputs.
        """
        import torch

        batch, topk = positions.shape
        if misses.shape != positions.shape or misses.dtype != torch.bool:
            raise ValueError("fetch miss mask must match the top-k selection")
        key = (layer, batch, topk)
        copier = self._copies.get(key)
        if copier is None or copier.output is not output:
            # Eager calls and distinct captures may allocate a new destination
            # with the same shape. Update the destination but retain the fixed
            # metadata that an existing graph may have captured during warmup.
            inputs = (
                copier.inputs
                if copier is not None
                else SparseCopyInputs(batch, topk, str(output.device))
            )
            copier = SparseKVCopy(
                self.manager.view(0, layer),
                self.manager.view(1, layer),
                inputs,
                block_dim=self.block_dim,
                kernel=self.kernel,
                output=output,
            )
            self._copies[key] = copier
        inputs = copier.inputs
        row_valid = (req_rows > 0) & (req_rows < row_decode_slot.numel())
        safe_rows = torch.where(row_valid, req_rows, 0).long()
        inputs.p_slots.copy_(row_prompt_slot[safe_rows])
        inputs.d_slots.copy_(row_decode_slot[safe_rows])
        inputs.prompt_lengths.copy_(row_prompt_len[safe_rows])
        inputs.decode_lengths.copy_(row_decode_len[safe_rows])
        inputs.positions.copy_(positions)
        inputs.valid.copy_(misses)
        inputs.active.copy_(row_valid & (row_decode_slot[safe_rows] >= 0))
        copier.gather()
