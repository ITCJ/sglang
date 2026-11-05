"""Fixed candidate locations for logical pages; allocation remains in the SR."""

from __future__ import annotations

import torch
from torch import Tensor


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
