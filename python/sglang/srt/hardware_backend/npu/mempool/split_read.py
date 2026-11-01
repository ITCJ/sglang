"""Assign logical prefix pages to readers independently of their storage layout."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from .prefix_layout import PrefixLayout

Page = TypeVar("Page")


def assign_pages(
    pages: Sequence[Page], readers: int, assignment: str
) -> list[list[Page]]:
    """Return rank-ordered page lists; padding is only a communication concern."""
    if assignment == "round_robin":
        return [list(pages[rank::readers]) for rank in range(readers)]
    if assignment == "contiguous":
        width, extra = divmod(len(pages), readers)
        bounds = [rank * width + min(rank, extra) for rank in range(readers + 1)]
        return [list(pages[start:end]) for start, end in zip(bounds, bounds[1:])]
    raise ValueError("prefix_read_assignment must be round_robin or contiguous")


def assign_prefix_pages(
    pages: Sequence[int], readers: int, assignment: str, layout: PrefixLayout | None
) -> list[list[int]]:
    """Assign request positions using their actual physical segment, without padding."""
    if assignment != "by_segment":
        return assign_pages(range(len(pages)), readers, assignment)
    assert layout is not None
    result: list[list[int]] = [[] for _ in range(readers)]
    for group, tasks in zip(layout.groups, layout.tasks(list(pages))):
        for rank, positions in zip(
            group, assign_pages([p for p, _ in tasks], len(group), "contiguous")
        ):
            result[rank] = positions
    return result
