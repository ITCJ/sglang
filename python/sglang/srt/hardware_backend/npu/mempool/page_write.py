"""Assign complete token pages to the writers of one shared prompt replica."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .split_read import assign_pages

if TYPE_CHECKING:
    from torch import Tensor


@dataclass(frozen=True)
class PageWriter:
    member: int
    members: int
    page_size: int
    assignment: str = "contiguous"

    def pages(self, tokens: int) -> list[int]:
        count = (tokens + self.page_size - 1) // self.page_size
        return assign_pages(range(count), self.members, self.assignment)[self.member]

    def rows(self, start: int, end: int, tokens: int) -> int:
        return sum(
            max(
                0,
                min(end, (page + 1) * self.page_size)
                - max(start, page * self.page_size),
            )
            for page in self.pages(tokens)
        )

    def mask(self, positions: Tensor, prompts: Tensor) -> Tensor:
        pages = positions // self.page_size
        if self.assignment == "round_robin":
            return pages % self.members == self.member
        count = (prompts + self.page_size - 1) // self.page_size
        width, extra = count // self.members, count % self.members
        start = self.member * width + extra.clamp(max=self.member)
        end = start + width + (extra > self.member).long()
        return (pages >= start) & (pages < end)
