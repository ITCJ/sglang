"""Temporary PD-disaggregation helpers for sparse KV offload.

This module intentionally keeps the admission policy and transfer-index
translation outside the generic PD path. It can be removed once sparse KV gets
first-class mem-pool support.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

SPARSE_KV_PD_PREFILL_SLOT_ATTR = "_sparse_kv_pd_prefill_slot"
SPARSE_KV_PD_DECODE_STAGING_ATTR = "_sparse_kv_pd_decode_staging_acquired"
SPARSE_KV_PD_TOKEN_COUNT_ATTR = "_sparse_kv_pd_token_count"


def is_sparse_kv_pd_enabled(transfer_backend, sparse_kv_manager) -> bool:
    return (
        sparse_kv_manager is not None
        and getattr(transfer_backend, "name", "") == "ASCEND"
    )


def resolve_pd_prefill_slot_count(scheduler) -> int:
    """Use configured prefill concurrency; default to one slot for this fallback."""
    server_args = getattr(scheduler, "server_args", None)
    slot_count = getattr(server_args, "prefill_max_requests", None)
    if slot_count is None:
        return 1
    return max(1, int(slot_count))


class SparseKVPDPrefillSlots:
    """Gate P-side prefill by temporary HBM source-buffer slots."""

    def __init__(self, manager, slot_count: int):
        self.manager = manager
        self.slot_count = int(slot_count)
        self.free_slots = deque(range(self.slot_count))

    def try_acquire(self, req) -> bool:
        if getattr(req, SPARSE_KV_PD_PREFILL_SLOT_ATTR, -1) >= 0:
            return True
        if not self.free_slots:
            return False
        setattr(req, SPARSE_KV_PD_PREFILL_SLOT_ATTR, self.free_slots.popleft())
        return True

    def bind_batch(self, reqs) -> None:
        self.manager.bind_pd_prefill_slots(reqs)

    def release(self, req) -> None:
        slot = getattr(req, SPARSE_KV_PD_PREFILL_SLOT_ATTR, -1)
        if slot < 0:
            return
        self.manager.clear_pd_prefill_slot_for_req(req)
        setattr(req, SPARSE_KV_PD_PREFILL_SLOT_ATTR, -1)
        if slot not in self.free_slots:
            self.free_slots.append(slot)

    def transfer_indices(self, req, start_idx: int, end_idx: int) -> np.ndarray:
        slot = getattr(req, SPARSE_KV_PD_PREFILL_SLOT_ATTR, -1)
        if slot < 0:
            raise RuntimeError(
                f"Sparse KV PD prefill slot is not acquired for request {req.rid}."
            )
        if end_idx < start_idx:
            return np.empty((0,), dtype=np.int32)
        return (
            np.arange(start_idx, end_idx, dtype=np.int64)
            + int(slot) * int(self.manager.max_context_len)
        ).astype(np.int32)


class SparseKVPDDecodeStaging:
    """Single-request D-side staging gate for temporary sparse-KV PD."""

    def __init__(self, manager, slot_count: int = 1):
        self.manager = manager
        self.slot_count = int(slot_count)
        if self.slot_count != 1:
            logger.warning(
                "Sparse KV PD temporary decode staging currently serializes "
                "requests; slot_count=%s is treated as one active slot.",
                self.slot_count,
            )
        self._owner = None

    def try_acquire(self, decode_req) -> bool:
        if getattr(decode_req.req, SPARSE_KV_PD_DECODE_STAGING_ATTR, False):
            return True
        if self._owner is not None:
            return False
        self._owner = decode_req
        setattr(decode_req.req, SPARSE_KV_PD_DECODE_STAGING_ATTR, True)
        return True

    def release(self, decode_req: Optional[object] = None) -> None:
        owner = decode_req or self._owner
        if owner is not None:
            req = getattr(owner, "req", owner)
            setattr(req, SPARSE_KV_PD_DECODE_STAGING_ATTR, False)
            setattr(req, SPARSE_KV_PD_TOKEN_COUNT_ATTR, 0)
        if decode_req is None or decode_req is self._owner:
            self._owner = None

    def transfer_indices(self, token_count: int) -> np.ndarray:
        return np.arange(int(token_count), dtype=np.int32)

    def mark_token_count(self, decode_req, token_count: int) -> None:
        setattr(decode_req.req, SPARSE_KV_PD_TOKEN_COUNT_ATTR, int(token_count))

    def offload_to_host(self, decode_req) -> None:
        token_count = int(getattr(decode_req.req, SPARSE_KV_PD_TOKEN_COUNT_ATTR, 0))
        if token_count <= 0:
            token_count = len(decode_req.req.origin_input_ids)
        self.manager.offload_pd_decode_staging_to_host(
            req_pool_idx=int(decode_req.req.req_pool_idx),
            token_count=token_count,
        )
