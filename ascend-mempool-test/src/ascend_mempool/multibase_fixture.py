"""Synthetic future-source topology for standalone benchmarks, never serving."""

from typing import Any

import torch
from torch import Tensor

from .multibase import BaseTable, MultibaseCopy


def mapped_regions(manager: Any, layer: int, region: str = "main") -> list[Any]:
    """Capture the union needed by 25, including inactive prefix segments."""
    views = [manager.view(0, layer, region="alive_" + region)]
    if region == "main":
        views.append(manager.view(1, layer))
    prefix = "prefix_" + region
    session = manager.session
    count = (
        len(session.prefix_layout.groups) if session and session.prefix_layout else 1
    )
    for segment in range(max(8, count)):
        views.append(
            manager.view(
                0,
                layer,
                region=prefix,
                segment=segment if session and session.prefix_layout else None,
            )
            if prefix in manager.layout.regions(0) and segment < count
            else None
        )
    return views


class PageLocations:
    """Reserve a canonical and a replica entry without implementing replication."""

    def __init__(self, slots: int, pages: int, side: int, device: str) -> None:
        self.canonical_region = side
        shape = (slots, pages, 2)
        self.regions = torch.full(shape, side, dtype=torch.int64, device=device)
        self.pages = torch.full(shape, -1, dtype=torch.int64, device=device)
        self.valid = torch.zeros(shape, dtype=torch.bool, device=device)
        self.selected = torch.zeros((slots, pages), dtype=torch.int64, device=device)

    def install(self, handle: int, pages: Tensor) -> None:
        self.regions[handle].fill_(self.canonical_region)
        self.pages[handle].fill_(-1)
        self.valid[handle].zero_()
        self.selected[handle].zero_()
        self.pages[handle, : pages.numel(), 0].copy_(pages)
        self.valid[handle, : pages.numel(), 0].fill_(True)

    def resolve(
        self, handles: Tensor, logical_pages: Tensor
    ) -> tuple[Tensor, Tensor, Tensor]:
        choice = self.selected[handles, logical_pages]
        primary = self.valid[handles, logical_pages, choice]
        # An invalidated replica falls back to the other reserved location.
        choice = torch.where(primary, choice, 1 - choice)
        return (
            self.regions[handles, logical_pages, choice],
            self.pages[handles, logical_pages, choice],
            self.valid[handles, logical_pages, choice],
        )


class BenchmarkCopy:
    """Single-source multibase read used only by the standalone bandwidth suite."""

    def __init__(self):
        self.plan = None
        self.key = None

    def __call__(
        self,
        source,
        target,
        si,
        di,
        valid,
        src_count,
        dst_count,
        row_bytes,
        count,
        block_dim,
        src_ptr=None,
        dst_ptr=None,
    ):
        key = (src_count, dst_count, row_bytes, count, block_dim)
        if self.key != key:
            self.plan = MultibaseCopy(
                BaseTable([(src_ptr or source.data_ptr(), src_count)], si.device),
                BaseTable([(dst_ptr or target.data_ptr(), dst_count)], si.device),
                count,
                row_bytes=row_bytes,
                block_dim=block_dim,
            )
            self.key = key
        self.plan.source.bases[0].fill_(src_ptr or source.data_ptr())
        self.plan.target.bases[0].fill_(dst_ptr or target.data_ptr())
        self.plan.submit(si, di, valid)
