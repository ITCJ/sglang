"""Per-replica completion accounting, driven only by scheduler-agreed facts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class PromptCoverage:
    """Join real fill and write completions without treating a hit as a write."""

    def __init__(
        self,
        tokens: int,
        hit: int,
        page_size: int,
        writers: int,
        groups: Sequence[Sequence[int]] = (),
        writer_pages: Sequence[Sequence[int]] = (),
    ) -> None:
        self.tokens, self.hit, self.page_size = tokens, hit, page_size
        self.groups = groups or [(r,) for r in range(writers)]
        self.rank_to_replica = {
            rank: index for index, group in enumerate(self.groups) for rank in group
        }
        self.writer_pages = writer_pages
        self.pages = [
            bytearray((tokens + page_size - 1) // page_size) for _ in self.groups
        ]
        self.ends = [hit] * writers
        self.filled = [not hit] * writers

    def observe(self, rank: int, start: int, end: int, filled: bool) -> None:
        # Runtime has already checked layer counts, Index K and contiguous writes.
        # Stage zero reports complete pages, with an end-page for small CPU fixtures.
        pages = self.pages[self.rank_to_replica[rank]]
        owned = self.writer_pages[rank] if self.writer_pages else range(len(pages))
        for page in owned:
            if (filled and (page + 1) * self.page_size <= self.hit) or (
                page * self.page_size >= start
                and min((page + 1) * self.page_size, self.tokens) <= end
            ):
                pages[page] = 1
        self.ends[rank], self.filled[rank] = self.hit + end - start, filled

    def snapshot(self) -> dict[str, Any]:
        contiguous = [
            next((i for i, done in enumerate(pages) if not done), len(pages))
            for pages in self.pages
        ]
        ready = min(self.tokens, min(contiguous) * self.page_size)
        # Independent old counter calculation, recorded on the same actual events.
        counter = min(self.ends) if all(self.filled) else 0
        ready = min(ready, counter)
        return dict(
            ready_tokens=ready,
            counter_tokens=counter,
            completed_pages=contiguous,
            written_ends=list(self.ends),
            fill_complete=list(self.filled),
        )


class ReplicaReaders:
    """Retain each replica until every named reader has stopped."""

    def __init__(self, readers: Sequence[Sequence[int]]) -> None:
        self.pending = [set(group) for group in readers]

    def stopped(self, reader: int) -> None:
        for pending in self.pending:
            pending.discard(reader)

    @property
    def counts(self) -> list[int]:
        return [len(pending) for pending in self.pending]

    @property
    def drained(self) -> bool:
        return not any(self.pending)
