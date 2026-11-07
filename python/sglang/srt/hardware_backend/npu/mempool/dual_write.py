"""Request page routes for prompt dual writes and direct prefix reads."""

from __future__ import annotations

from typing import Any

import torch

from .multibase import BaseTable, MultibaseCopy, view_regions


class DualWrite:
    def __init__(self, manager: Any, device: str) -> None:
        self.manager = manager
        self.size = manager.layout.prompt.page_size
        shape = (manager.layout.prompt.slots, manager.layout.prompt.tokens // self.size)
        self.regions = torch.full(shape, -1, dtype=torch.int64, device=device)
        self.pages = torch.full_like(self.regions, -1)
        self.candidates = torch.full_like(self.regions, -1)
        self.decode_offsets = torch.zeros(shape[0], dtype=torch.int64, device=device)
        self.prefix_layout = manager.session.prefix_layout
        # SparseKVCopy source order: P, D, cache HBM, current HBM, prefix segments.
        self.candidate_counts = torch.zeros(
            (
                manager.layout.prompt.layers + manager.layout.prefix_index.layers,
                shape[0],
            ),
            dtype=torch.int64,
            device=device,
        )
        self.expected_counts: dict[int, int] = {}

    def install(
        self,
        handle: int,
        request: dict[str, Any],
        decode: dict[str, Any],
        candidates: list[int],
    ) -> None:
        hit = request["hit_len"] // self.size
        prompt = request["prompt_tokens"] // self.size
        self.regions[handle].fill_(-1)
        self.pages[handle].fill_(-1)
        self.candidates[handle].fill_(-1)
        for i, source in enumerate(request["prefix_sources"]):
            segment, offset = self.prefix_layout.decode(source["logical_page"])
            self.regions[handle, i] = 4 + segment
            self.pages[handle, i] = offset
        for i in range(hit, prompt):
            self.regions[handle, i] = 1
            self.pages[handle, i] = decode["pages"][i - hit]["physical_page"]
        for source, target in zip(request["candidate_sources"], candidates):
            self.candidates[handle, source] = target
        self.decode_offsets[decode["slot"]] = prompt - hit
        self.candidate_counts[:, handle].zero_()
        self.expected_counts[handle] = (
            sum(
                self.prefix_layout.groups[self.prefix_layout.decode(p)[0]][0]
                == self.manager.session.tp_rank
                for p in candidates
            )
            * self.size
        )

    def prefix_views(self, layer: int, region: str = "prefix_main") -> list[Any]:
        return [
            self.manager.view(0, layer, region=region, segment=segment)
            for segment in range(len(self.prefix_layout.groups))
        ]

    def write(
        self,
        layer: int,
        values: Any,
        slots: Any,
        positions: Any,
        valid: Any,
        remote_valid: Any,
        *,
        index: bool = False,
    ) -> tuple[MultibaseCopy, Any]:
        """Submit both main targets together; completion follows the caller's event."""
        safe_slots = torch.where(valid, slots, 0)
        logical = torch.where(valid, positions // self.size, 0)
        candidate = self.candidates[safe_slots, logical]
        candidate_valid = valid & (candidate >= 0)
        safe_candidate = candidate.clamp_min(0)
        segment = safe_candidate // self.prefix_layout.segment_pages
        offset = safe_candidate % self.prefix_layout.segment_pages
        rank = self.manager.session.tp_rank
        owners = torch.tensor(
            [group[0] for group in self.prefix_layout.groups],
            dtype=torch.int64,
            device=values.device,
        )
        candidate_valid &= owners[segment] == rank
        counter = layer + (self.manager.layout.prompt.layers if index else 0)
        self.candidate_counts[counter].scatter_add_(
            0, safe_slots, candidate_valid.long()
        )
        views = self.prefix_views(layer, "prefix_index" if index else "prefix_main")
        if not index:
            views = [self.manager.view(1, layer), *views]
        row_bytes = views[0].layout.row_bytes
        source = BaseTable(
            [(values.data_ptr(), values.shape[0])], values.device, [values]
        )
        target = BaseTable(
            view_regions(views, row_bytes), values.device, [self.manager]
        )
        starts = target.ranges[:, 0]
        target_row = (
            offset * views[-1].layout.layers * self.size
            + positions % self.size
            + starts[segment + (0 if index else 1)]
        )
        source_row = torch.arange(
            values.shape[0], dtype=torch.int64, device=values.device
        )
        mask = candidate_valid
        remote_valid = remote_valid & (self.pages[safe_slots, logical] >= 0)
        if not index:
            remote_page = self.pages[safe_slots, logical]
            remote_row = (
                remote_page * views[0].layout.layers * self.size + positions % self.size
            )
            target_row = torch.cat((remote_row, target_row))
            source_row = torch.cat((source_row, source_row))
            mask = torch.cat((remote_valid, mask))
        transfer = MultibaseCopy(
            source, target, source_row.numel(), row_bytes=row_bytes
        )
        transfer.submit(source_row, target_row, mask)
        # The caller retains this metadata until the forward completion event.
        return transfer, remote_valid

    def check_candidates(self, handle: int) -> None:
        if not bool(
            (
                self.candidate_counts[:, handle].cpu() == self.expected_counts[handle]
            ).all()
        ):
            raise RuntimeError(
                "dual-write candidate coverage differs from reserved pages"
            )

    def remote_mask(self, handles: Any, positions: Any, prompts: Any) -> Any:
        logical = (
            positions.clamp(min=0, max=self.manager.layout.prompt.tokens - 1)
            // self.size
        )
        return (positions < prompts) & (
            self.regions[handles.clamp_min(0)[:, None], logical] >= 4
        )

    def prompt_location(self, handle: int, logical: int, layer: int) -> dict[str, Any]:
        region = int(self.regions[handle, logical].item())
        page = int(self.pages[handle, logical].item())
        segment = region - 4 if region >= 4 else None
        kind = "prefix_main" if segment is not None else "alive_main"
        side = 0 if segment is not None else 1
        view = self.manager.view(side, layer, region=kind, segment=segment)
        return dict(
            source="prefix_remote" if side == 0 else "D_local",
            region=kind,
            physical_page=page,
            segment=segment,
            owner_participant=view.participant,
            pool_rank=side,
        )
