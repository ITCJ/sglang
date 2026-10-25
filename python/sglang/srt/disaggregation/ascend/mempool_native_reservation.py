"""Own D's native rows/pages from Router alloc through the final device drain.

Reserved capacity is deliberately separate from Req's effective KV lengths.
Radix, speculation and retraction are disabled in this mode; consequently one
owner can return every reserved page once, including an unused partial tail.
"""

from __future__ import annotations

from typing import Any, Callable

import msgspec
import torch


class NativeReservation(msgspec.Struct):
    row: int
    indices: torch.Tensor
    prompt_tokens: int
    limit: int
    req: Any = None


class NativeReservations:
    def __init__(
        self,
        rows: Any,
        allocator: Any,
        *,
        reset_rows: Callable[[list[int]], None] = lambda rows: None,
    ) -> None:
        self.rows = rows
        self.allocator = allocator
        self.reset_rows = reset_rows
        self.reservations: dict[int, NativeReservation] = {}

    @staticmethod
    def applies_to(reqs: list[Any]) -> bool:
        """Only explicit native fake warmup may use the ordinary allocator path.

        Missing reservations must still fail for real requests. In particular,
        checking owns() here would silently admit undispatched backend traffic.
        """
        fake = [getattr(req, "bootstrap_host", None) == "2.2.2.2" for req in reqs]
        if any(fake) and not all(fake):
            raise RuntimeError("fake warmup cannot share a batch with Router requests")
        return not any(fake)

    def reserve(
        self, room: int, prompt_tokens: int, decode_tokens: int
    ) -> dict[str, int]:
        """Take physical resources now, before an HTTP Req exists."""
        if room in self.reservations or self.reservations:
            raise ValueError("native reservation requires an empty serial worker")
        limit = prompt_tokens + decode_tokens
        page_size = self.allocator.page_size
        tokens = (limit + page_size - 1) // page_size * page_size
        if tokens > self.rows.req_to_token.shape[1]:
            raise ValueError("native reservation exceeds the request row width")
        row_ids = self.rows.alloc_rows(1)
        if row_ids is None:
            raise RuntimeError("native request row capacity exhausted")
        # alloc takes TOKEN count, not page count. Do not assume adjacent pages.
        indices = self.allocator.alloc(tokens)
        if indices is None:
            self.rows.free_rows(row_ids)
            raise RuntimeError("native Index K page capacity exhausted")
        self.reservations[room] = NativeReservation(
            row_ids[0], indices, prompt_tokens, limit
        )
        # alloc_rows bypasses sparse_cache's legacy alloc(Req) wrapper.
        self.reset_rows(row_ids)
        self.rows.write((row_ids[0], slice(0, tokens)), indices)
        return {"row": row_ids[0], "pages": tokens // page_size, "tokens": tokens}

    def owns(self, req: Any) -> bool:
        reservation = self.reservations.get(req.bootstrap_room)
        return reservation is not None and (
            reservation.req is None or reservation.req is req
        )

    def attach(self, req: Any, fill_len: int) -> torch.Tensor:
        """Use the reserved indices as native Index K transfer destinations."""
        reservation = self.reservations[req.bootstrap_room]
        if reservation.req is not None or req.kv.req_pool_idx is not None:
            raise ValueError("native reservation is already attached")
        if fill_len != reservation.prompt_tokens or req.output_ids:
            raise ValueError("native reservation only supports a cold prompt")
        reservation.req = req
        req.kv.req_pool_idx = reservation.row
        req.kv.kv_committed_len = fill_len
        req.kv.kv_allocated_len = fill_len
        req.full_untruncated_fill_ids = req.origin_input_ids[:]
        req.prefix_indices = torch.empty((0,), dtype=torch.int64)
        req.set_extend_range(0, fill_len)
        return reservation.indices[:fill_len]

    def can_decode(self, reqs: list[Any]) -> bool:
        """Capacity comes from this request's reservation, not global free pages."""
        if len(reqs) > 1:
            raise RuntimeError("Router mode permits one real decode request")
        for req in reqs:
            reservation = self.reservations.get(req.bootstrap_room)
            if (
                reservation is None
                or reservation.req is not req
                or req.kv.req_pool_idx != reservation.row
                or req.kv.kv_allocated_len >= reservation.limit
            ):
                return False
        return True

    def decode(self, batch: Any, token_per_req: int) -> torch.Tensor:
        """Consume the next reserved token; no allocator call or device sync."""
        if (
            token_per_req != 1
            or len(batch.reqs) != 1
            or not self.can_decode(batch.reqs)
        ):
            raise RuntimeError("decode exceeded its native reservation")
        req = batch.reqs[0]
        reservation = self.reservations[req.bootstrap_room]
        pos = req.kv.kv_allocated_len
        if int(batch.seq_lens_cpu[0]) != pos or req.kv.kv_committed_len != pos:
            raise RuntimeError("decode position differs from reserved native row")
        req.kv.kv_allocated_len += 1
        req.kv.kv_committed_len += 1
        return reservation.indices[pos : pos + 1]

    def release(self, req: Any) -> None:
        """Called after device drain; return used and unused pages together."""
        reservation = self.reservations.get(req.bootstrap_room)
        if reservation is None:
            if req.kv.req_pool_idx is not None:
                raise RuntimeError("native reservation disappeared before cleanup")
            return
        if reservation.req is not req or req.kv.req_pool_idx != reservation.row:
            raise ValueError("native cleanup does not own the reserved row")
        self.allocator.free(reservation.indices)
        self.rows.write((reservation.row, slice(0, len(reservation.indices))), 0)
        # Keep native row-free hooks (including sparse metadata cleanup).
        self.rows.free(req)
        req.kv.mark_kv_released()
        del self.reservations[req.bootstrap_room]
