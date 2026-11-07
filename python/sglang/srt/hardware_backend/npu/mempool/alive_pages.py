"""Physical pages owned by a request until its existing drain/free handshake."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageLease:
    request: str
    version: int
    pages: tuple[int, ...]


class AlivePages:
    """One region's free pages; shared ranks replay the same allocation order."""

    def __init__(self, capacity: int, *, page_size: int) -> None:
        self.capacity = capacity
        self.page_size = page_size
        self._free = set(range(capacity))
        self._leases: dict[str, PageLease] = {}
        self._version = 0

    @property
    def free_count(self) -> int:
        return len(self._free)

    def reserve(self, request: str, tokens: int) -> PageLease:
        count = (tokens + self.page_size - 1) // self.page_size
        if request in self._leases:
            raise RuntimeError("request already owns alive pages")
        if count < 0 or count > self.free_count:
            raise ValueError("alive page capacity exhausted")
        pages = tuple(sorted(self._free)[:count])
        self._version += 1
        lease = PageLease(request, self._version, pages)
        self._free.difference_update(pages)
        self._leases[request] = lease
        return lease

    def release(self, lease: PageLease) -> None:
        if self._leases.get(lease.request) != lease:
            raise RuntimeError("stale alive page lease")
        del self._leases[lease.request]
        self._free.update(lease.pages)
