"""Build prompt/decode UniDexCopy indices using graph-updatable device inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import msgspec

from .layout import UINT32_MAX, positive_int
from .manager import MempoolKVManager, MempoolKVView
from .multibase import BaseTable, MultibaseCopy, view_regions

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
        output: Tensor | None = None,
        extra_sources: tuple[Any, Any] = (None, None),
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
        self.views = (prompt, decode)
        regions = view_regions(self.views, prompt.layout.row_bytes)
        for tensor in extra_sources:
            regions.append(
                (
                    tensor.data_ptr(),
                    tensor.numel() * tensor.element_size() // prompt.layout.row_bytes,
                )
                if tensor is not None
                else (0, 1)
            )
        self.dual_write = prompt.owner.dual_write
        if self.dual_write is not None:
            regions.extend(
                view_regions(
                    self.dual_write.prefix_views(prompt.layer), prompt.layout.row_bytes
                )
            )
        self.extra_sources = extra_sources
        self.source_table = BaseTable(
            regions, output.device, [prompt.owner, *extra_sources]
        )
        self.target_table = BaseTable(
            [(output.data_ptr(), inputs.positions.numel())], output.device, [output]
        )
        self.transfer = MultibaseCopy(
            self.source_table,
            self.target_table,
            inputs.positions.numel(),
            row_bytes=prompt.layout.row_bytes,
            block_dim=block_dim,
            kernel=kernel,
        )

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
            & (inputs.prompt_lengths <= self.prompt.token_capacity)
            & (inputs.decode_lengths >= 0)
            & (inputs.decode_lengths <= self.decode.token_capacity)
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
            p_valid,
            inputs.p_slots.unsqueeze(1) * self.prompt.token_capacity + position,
            0,
        )
        d_index = torch.where(
            d_valid,
            inputs.d_slots.unsqueeze(1) * self.decode.token_capacity + decode_position,
            0,
        )
        return (
            CopyIndices(p_index.flatten(), inputs.dst_index, p_valid.flatten()),
            CopyIndices(d_index.flatten(), inputs.dst_index, d_valid.flatten()),
        )

    def gather(
        self,
        observe: bool = False,
        layer_id: int = 0,
        extra_routes: tuple[CopyIndices, ...] = (),
    ) -> Tensor:
        """Materialize exactly one selected source per row in a single launch."""
        import torch

        source_rows = torch.zeros_like(self.inputs.dst_index)
        valid = torch.zeros_like(source_rows, dtype=torch.bool)
        for side, (view, route) in enumerate(zip(self.views, self.routes())):
            if side == 0 and self.dual_write is not None:
                handle = route.src_index // view.token_capacity
                position = route.src_index % view.token_capacity
                logical = position // view.layout.page_size
                region = self.dual_write.regions[handle, logical]
                page = self.dual_write.pages[handle, logical]
                safe_region = region.clamp_min(0)
                physical = (
                    page * view.layout.layers * view.layout.page_size
                    + position % view.layout.page_size
                )
                start = self.source_table.ranges[safe_region, 0]
                end = self.source_table.ranges[safe_region, 1]
            else:
                physical = view.map_rows(route.src_index)
                start, end = self.source_table.ranges[side]
            enabled = route.valid & (physical >= 0) & (physical < end - start)
            source_rows = torch.where(enabled, start + physical, source_rows)
            valid |= enabled
        for offset, route in enumerate(extra_routes):
            region = len(self.views) + offset
            source_rows = torch.where(
                route.valid,
                route.src_index + self.source_table.starts[region],
                source_rows,
            )
            valid |= route.valid
        if observe:
            stamp = torch.npu.mstx.range_start(
                f"mempool:materialize:{layer_id}", torch.npu.current_stream()
            )
        self.transfer.submit(source_rows, self.inputs.dst_index, valid)
        if observe:
            torch.npu.mstx.range_end(stamp)
        return self.output


class KVFetch:
    """Fetch cache misses directly into the caller's selected KV."""

    def __init__(
        self, manager: MempoolKVManager, block_dim: int, kernel: Any = None
    ) -> None:
        self.manager = manager
        self.block_dim = block_dim
        self.kernel = kernel
        self.observe = False
        self.start_layer = 0
        self._copies: dict[tuple[int, int, int], SparseKVCopy] = {}
        self._captured: dict[tuple[int, int, int, int], SparseKVCopy] = {}

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
        *,
        extra_sources: tuple[Any, Any] = (None, None),
        extra_routes: tuple[CopyIndices, ...] = (),
    ) -> SparseKVCopy:
        """Materialize every selected source on the miss stream, including zero work.

        The caller orders binding/current-layer writes before this stream, and
        joins it before attention/refill or the next use of these fixed inputs.
        """
        import torch

        batch, topk = positions.shape
        if misses.shape != positions.shape or misses.dtype != torch.bool:
            raise ValueError("fetch miss mask must match the top-k selection")
        key = (layer, batch, topk)
        copier = self._copies.get(key)
        if copier is None or copier.output.data_ptr() != output.data_ptr():
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
                extra_sources=extra_sources,
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
        if output.device.type == "npu" and torch.npu.is_current_stream_capturing():
            self._captured[(*key, output.data_ptr())] = copier
        copier.gather(self.observe, layer + self.start_layer, extra_routes)
        return copier
