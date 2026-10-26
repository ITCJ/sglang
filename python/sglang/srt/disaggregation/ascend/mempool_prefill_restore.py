"""Give P's native prefill a request-owned restored prefix, without radix retention."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import torch

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.prefix import PrefixStore


class PrefillRestore:
    """Own restored indices until native prefill attaches them to its request row.

    Ordinary ChunkCache cleanup frees the whole row, including the restored
    pages. This owner never frees pages again and survives that native cleanup
    while fill still reads the independent prefix store.
    """

    def __init__(
        self,
        allocator: Any,
        store: PrefixStore,
        *,
        force_noncontiguous: bool = False,
        delay_fill: bool = False,
    ) -> None:
        self.allocator, self.store = allocator, store
        self.force_noncontiguous = force_noncontiguous
        self.delay_fill = delay_fill
        self.request: dict[str, Any] | None = None
        self.slot = -1
        self.indices: torch.Tensor | None = None
        self.ready = False
        self.native_freed = False

    def begin_request(self, request: dict[str, Any], *, slot: int) -> None:
        if self.request is not None:
            raise RuntimeError("prefill restore still owns a request")
        self.request = copy.deepcopy(request)
        self.slot = slot
        self.ready = request["hit_len"] == 0
        self.native_freed = False

    def load_back(
        self, *, op_id: int, source_pages: list[int]
    ) -> dict[str, Any] | None:
        request = self.request
        if (
            request is None
            or request["hit_len"] != len(source_pages) * self.allocator.page_size
        ):
            raise ValueError("restore pages differ from the admitted request")
        if not source_pages or self.native_freed:
            raise ValueError("load_back requires a live prefix hit")
        if self.indices is None:
            # Acceptance deliberately leaves one allocator page out of the map.
            # No synthetic address is introduced; normal serving allocates once.
            gap = (
                self.allocator.page_size
                if self.force_noncontiguous
                and request["hit_len"] > self.allocator.page_size
                else 0
            )
            allocation = self.allocator.alloc(request["hit_len"] + gap)
            if allocation is None:
                raise RuntimeError("native prefix allocation exhausted")
            if gap:
                self.indices = torch.cat((allocation[:gap], allocation[2 * gap :]))
                self.allocator.free(allocation[gap : 2 * gap])
            else:
                self.indices = allocation
        receipt: dict[str, Any] | None = self.store.load_back(
            rid=request["rid"],
            op_id=op_id,
            slot=self.slot,
            source_pages=source_pages,
            indices=self.indices,
            pool=self.allocator.get_kvcache(),
        )
        if receipt is not None:
            self.ready = True
        return receipt

    def _check_request(self, req: Any) -> dict[str, Any]:
        request = self.request
        if request is None or (
            req.rid != request["rid"]
            or req.bootstrap_room != request["room"]
            or list(req.origin_input_ids) != request["input_ids"]
        ):
            raise ValueError("native prefix belongs to a different request")
        return request

    def match(self, req: Any) -> torch.Tensor:
        request = self._check_request(req)
        if not self.ready or self.native_freed:
            raise RuntimeError("native prefix has not been restored or was released")
        if request["hit_len"] == 0:
            return torch.empty((0,), dtype=torch.int64)
        assert self.indices is not None
        return self.indices

    def native_released(self, req: Any) -> None:
        self._check_request(req)
        self.native_freed = True

    def finish_request(self) -> None:
        if not self.native_freed:
            raise RuntimeError("restored request still owns native storage")
        self.request, self.indices = None, None
        self.ready = False


def install_prefill_cache(scheduler: Any, restore: PrefillRestore) -> None:
    """Use the native cache interface so its normal extend allocator consumes the map."""
    from sglang.srt.mem_cache.base_prefix_cache import MatchPrefixParams, MatchResult
    from sglang.srt.mem_cache.cache_init_params import CacheInitParams
    from sglang.srt.mem_cache.chunk_cache import ChunkCache

    if type(scheduler.tree_cache) is not ChunkCache:
        raise ValueError(
            "mempool prefix restore requires the ordinary disabled radix cache"
        )

    class RestoredChunkCache(ChunkCache):  # type: ignore[misc]
        def match_prefix(self, params: MatchPrefixParams) -> MatchResult:
            if params.req is None:
                raise ValueError("restored prefix matching requires its request")
            if params.req.bootstrap_host == "2.2.2.2":
                return super().match_prefix(params)
            indices = restore.match(params.req)
            if len(indices) > len(params.key):
                raise ValueError(
                    "native matching limit is smaller than the restored prefix"
                )
            return MatchResult(
                device_indices=indices,
                last_device_node=None,
                last_host_node=None,
                best_match_node=None,
                cache_protected_len=0,
            )

    scheduler.tree_cache = RestoredChunkCache(
        CacheInitParams(
            disable=True,
            req_to_token_pool=scheduler.req_to_token_pool,
            token_to_kv_pool_allocator=scheduler.token_to_kv_pool_allocator,
            page_size=scheduler.server_args.page_size,
        )
    )
