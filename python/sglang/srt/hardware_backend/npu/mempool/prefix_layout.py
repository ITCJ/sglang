"""Describe a single prefix replica split across equal NUMA segments."""

from __future__ import annotations

from dataclasses import dataclass

from .layout import check_index


@dataclass(frozen=True)
class PrefixLayout:
    pages: int
    groups: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if not self.groups or self.pages <= 0 or self.pages % len(self.groups):
            raise ValueError("prefix pages must divide evenly across nonempty segments")

    @property
    def segment_pages(self) -> int:
        return self.pages // len(self.groups)

    def decode(self, page: int) -> tuple[int, int]:
        check_index("prefix page", page, self.pages)
        return divmod(page, self.segment_pages)

    def select(self, free: set[int], count: int, start: int) -> list[int]:
        """Interleave new pages, falling back to the segment with most free pages."""
        available = [
            sorted(p for p in free if self.decode(p)[0] == segment)
            for segment in range(len(self.groups))
        ]
        selected = []
        for index in range(min(count, len(free))):
            segment = (start + index) % len(self.groups)
            if not available[segment]:
                segment = max(range(len(available)), key=lambda s: len(available[s]))
            selected.append(available[segment].pop(0))
        return selected

    def tasks(self, pages: tuple[int, ...] | list[int]) -> list[list[tuple[int, int]]]:
        """Pair request page positions with local offsets for each segment."""
        result: list[list[tuple[int, int]]] = [[] for _ in self.groups]
        for position, page in enumerate(pages):
            segment, offset = self.decode(page)
            result[segment].append((position, offset))
        return result
