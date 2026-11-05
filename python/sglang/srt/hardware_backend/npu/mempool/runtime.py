"""Adapt model forwards to BM writes without owning PD protocol transitions."""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from typing import Any, Protocol

import msgspec
import torch
from sglang.srt.hardware_backend.npu.kv_rows import derive_kv_rows
from torch import Tensor

from .config import MempoolConfig
from .copy import CopyIndices, KVFetch
from .decode_write import DecodeKV, DecodeWriter
from .diagnostics import diagnostics_enabled, startup_stage
from .layout import check_index, positive_int
from .manager import MempoolKVManager
from .offload import MempoolKVOffload
from .page_write import PageWriter
from .prefix import PrefixStore

logger = logging.getLogger(__name__)


class WriteEvent(Protocol):
    """Represent completion on a device submission stream."""

    def record(self) -> None:
        """Record after preceding binding updates or KV write operations."""
        ...

    def query(self) -> bool:
        """Return whether all recorded work has completed."""
        ...


class KVWriteExpectation(msgspec.Struct, frozen=True):
    """Describe real rows processed this forward, in full-sequence coordinates."""

    req_pool_idx: int
    start_position: int
    rows: int


class KVRowBinding(msgspec.Struct, frozen=True, eq=False):
    """Identify one local row attachment, even if its coordinates are later reused.

    Keep the object returned by bind with the approved request attempt. Object
    identity distinguishes attachments without importing PD sessions/generations.
    This is not slot ownership: only control may acquire or release a slot.
    """

    req_pool_idx: int
    slot: int
    prompt_tokens: int
    prompt_slot: int | None = None
    write_start: int = 0
    page_versions: tuple[tuple[int, int, int], ...] = ()


class KVWriteReceipt(msgspec.Struct, frozen=True):
    """Retain completed local progress after the request row has been detached."""

    binding: KVRowBinding
    submitted: int
    completed: int
    main_written: int = 0
    index_written: int = 0


class _Binding(msgspec.Struct):
    """Keep local submission/completion progress independent of output tokens."""

    attachment: KVRowBinding
    submitted: int = 0
    completed: int = 0
    forwards: int = 0
    replay_forwards: int = 0
    prompt_bm_rows: int = 0
    decode_bm_rows: int = 0
    main_written: int = 0
    index_written: int = 0


class _Forward(msgspec.Struct):
    """Track one Python forward boundary, including graph replay submissions."""

    writes: tuple[KVWriteExpectation, ...]
    replay: bool
    capture: bool
    layers: set[int] = msgspec.field(default_factory=set)
    selection_layers: set[int] = msgspec.field(default_factory=set)
    fetch_layers: set[int] = msgspec.field(default_factory=set)
    index_layers: set[int] = msgspec.field(default_factory=set)
    retained: list[Tensor] = msgspec.field(default_factory=list)


class _Completion(msgspec.Struct):
    """Retain an ordered count snapshot until its device event completes."""

    event: WriteEvent
    snapshot: Tensor
    rows: tuple[int, ...]
    totals: tuple[int, ...]
    slot_totals: tuple[int, ...]
    replay: bool
    fetch: Tensor | None
    sources: Tensor | None = None
    index_snapshot: Tensor | None = None
    retained: tuple[Tensor, ...] = ()
    range_errors: Tensor | None = None
    index_totals: tuple[int, ...] | None = None
    observation: Any = None


class MempoolRuntime:
    """Own binding tensors and per-layer writers for one attention backend.

    The scheduler calls begin/end_forward around every eager forward or graph
    replay on its submission stream. write_layer runs once per layer (only at
    capture time for graphs). Bind/detach require tick approval and a row with
    no outstanding local work or future host submissions. P can detach after
    native handoff is safe while control keeps its mempool slot until D's DONE.
    D detaches only after its whole-side drain. The caller proves these external
    conditions; a completed local event alone does not establish native safety.
    This adapter never sends control messages or releases protocol ownership.
    """

    def __init__(
        self,
        manager: MempoolKVManager,
        *,
        req_pool_rows: int,
        max_context_len: int,
        start_layer: int,
        device: str,
        mlapo_enabled: bool = False,
        block_dim: int = 48,
        kernels: Sequence[Any] | None = None,
        event_factory: Callable[[], WriteEvent] | None = None,
        wait_event: Callable[[WriteEvent], None] | None = None,
        fetch_enabled: bool = False,
        fetch_kernel: Any = None,
        source_evidence: bool = False,
        index_pool: Any = None,
        index_kernels: Sequence[Any] | None = None,
        prefix_store: PrefixStore | None = None,
        page_writer: PageWriter | None = None,
        index_writer: PageWriter | None = None,
        decode_writer: DecodeWriter | None = None,
    ) -> None:
        """Allocate stable bindings/counters after BM mappings are ready."""
        if mlapo_enabled:
            raise ValueError("mempool does not support MLAPO")
        positive_int("req_pool_rows", req_pool_rows)
        positive_int("max_context_len", max_context_len)
        if type(start_layer) is not int or start_layer < 0:
            raise ValueError("start_layer must be a nonnegative integer")
        self.config: MempoolConfig | None = None
        self.observer: Any = None
        self.observation_device: Any = None
        self.observation_forward: dict[int, dict[str, Any]] = {}
        self.observation_hashes: dict[str, list[str]] = {}
        self.observation_trace: Any = None
        self.observation_sample_every = 1
        self.observation_page_size = manager.layout.prompt.page_size
        self.observation_producers: tuple[int, ...] = ()
        self.observation_config: Any = None
        self.observation_hot_cache_tokens = 0
        self.manager = manager
        self.prefix_store = prefix_store
        self.page_writer = page_writer
        self.index_writer = index_writer
        manager.bases(manager.rank)
        self.layout = manager.layout.layout_for_rank(manager.rank)
        if kernels is not None and len(kernels) != self.layout.layers:
            raise ValueError("one kernel is required per local layer")
        self.start_layer = start_layer
        self.max_context_len = max_context_len
        self.row_slot = torch.full(
            (req_pool_rows,), -1, dtype=torch.int64, device=device
        )
        self.row_prompt_len = torch.full_like(self.row_slot, -1)
        self.row_prompt_slot = torch.full_like(self.row_slot, -1)
        self.row_decode_len = torch.zeros_like(self.row_slot)
        self.row_write_start = torch.zeros_like(self.row_slot)
        self.row_write_end = torch.zeros_like(self.row_slot)
        self._range_errors = torch.zeros_like(self.row_slot)
        if fetch_enabled and manager.rank != 1:
            raise ValueError("BM fetch requires decode rank")
        self._fetch = (
            KVFetch(manager, block_dim, fetch_kernel) if fetch_enabled else None
        )
        # Per-layer/row coverage and invalid count/position.
        # Read only a completed snapshot, never synchronize inside captured work.
        self._fetch_checks = (
            torch.zeros(
                (self.layout.layers, req_pool_rows, 3), dtype=torch.int64, device=device
            )
            if fetch_enabled
            else None
        )
        self._fetch_sources = (
            torch.zeros(
                (self.layout.layers, req_pool_rows, 2), dtype=torch.int64, device=device
            )
            if fetch_enabled and source_evidence
            else None
        )
        self._write_counts = torch.zeros(
            (self.layout.layers, self.layout.slots), dtype=torch.int32, device=device
        )
        self._writers = [
            MempoolKVOffload(
                manager.view(manager.rank, layer),
                block_dim=block_dim,
                kernel=kernels[layer] if kernels is not None else None,
                feature_part=decode_writer.feature_part
                if decode_writer is not None
                else (0, 1),
            )
            for layer in range(self.layout.layers)
        ]
        self._decode = (
            DecodeKV(
                manager,
                decode_writer,
                req_pool_rows,
                device,
                fetch_kernel,
            )
            if decode_writer is not None
            else None
        )
        self._index_pool = index_pool
        self._index_writers: dict[int, MempoolKVOffload] = {}
        self._index_counts: Tensor | None = None
        index_layout = manager.layout.prompt_index if manager.rank == 0 else None
        if index_layout is not None:
            if index_pool is None or index_pool.index_k_scale_buffer is not None:
                raise ValueError("prefix requires the native unscaled Index K pool")
            producers = tuple(index_pool.indexer_layer_ids)
            if (
                len(set(producers)) != index_layout.layers
                or len(producers) != index_layout.layers
                or index_pool.index_head_dim != index_layout.dim
                or any(
                    not start_layer <= layer < start_layer + self.layout.layers
                    for layer in producers
                )
                or (index_kernels is not None and len(index_kernels) != len(producers))
            ):
                raise ValueError("native Index K geometry differs from DRAM layout")
            self._index_counts = torch.zeros(
                (index_layout.layers, self.layout.slots),
                dtype=torch.int32,
                device=device,
            )
            for index, layer in enumerate(producers):
                self._index_writers[layer] = MempoolKVOffload(
                    manager.view(0, index, region="alive_index"),
                    block_dim=block_dim,
                    kernel=index_kernels[index] if index_kernels is not None else None,
                )
        if event_factory is None:
            if self.row_slot.device.type != "npu":
                raise ValueError("CPU runtime tests must supply a completion event")
            event_factory = lambda: torch.npu.Event()
        self._event_factory = event_factory
        if wait_event is None:
            if self.row_slot.device.type == "npu":
                wait_event = lambda event: torch.npu.current_stream().wait_event(event)
            else:
                wait_event = lambda event: None
        self._wait_event = wait_event
        self.fault: str | None = None
        self._prepared: tuple[KVWriteExpectation, ...] | None = None
        self._bindings: dict[int, _Binding] = {}
        self._slot_totals = [0] * self.layout.slots
        self._index_slot_totals = [0] * self.layout.slots
        self._pending: deque[_Completion] = deque()
        self._active: _Forward | None = None
        self._capture_validated = False
        self._logged_replay = False
        self._binding_update: WriteEvent | None = None
        self._record_binding_update()

    @property
    def fetch_enabled(self) -> bool:
        return self._fetch is not None

    def _row(self, row: int) -> None:
        """Reject the permanently invalid padding row and out-of-range indices."""
        check_index("request row", row, self.row_slot.numel())
        if row == 0:
            raise ValueError("request row 0 is reserved for graph padding")

    def _row_in_flight(self, row: int) -> bool:
        """Include both the current host submission and recorded completions."""
        return (
            self._active is not None
            and any(write.req_pool_idx == row for write in self._active.writes)
        ) or any(row in completion.rows for completion in self._pending)

    def _record_binding_update(self) -> None:
        """Order scheduler-side binding writes before the next forward stream."""
        self._binding_update = self._event_factory()
        self._binding_update.record()

    def bind(
        self,
        req_pool_idx: int,
        *,
        slot: int,
        prompt_tokens: int,
        prompt_slot: int | None = None,
        start_position: int = 0,
    ) -> KVRowBinding:
        """Install an approved allocation and return its local attachment identity."""
        if self.fault is not None:
            raise RuntimeError(f"mempool runtime fault: {self.fault}")
        if self._active is not None:
            raise RuntimeError("cannot change bindings during an open forward")
        self._row(req_pool_idx)
        check_index("slot", slot, self.layout.slots)
        positive_int("prompt_tokens", prompt_tokens)
        if prompt_tokens > self.manager.layout.prompt.tokens:
            raise ValueError("prompt length exceeds P mempool capacity")
        if (
            type(start_position) is not int
            or not 0 <= start_position < prompt_tokens
            or (self.manager.rank == 1 and start_position != 0)
        ):
            raise ValueError("invalid prefill write range")
        if self.manager.rank == 1:
            if prompt_slot is None:
                raise ValueError("decode requires the approved prompt slot")
            check_index("prompt slot", prompt_slot, self.manager.layout.prompt.slots)
        if req_pool_idx in self._bindings or self._row_in_flight(req_pool_idx):
            raise RuntimeError("request row is already bound or in-flight")
        if any(binding.attachment.slot == slot for binding in self._bindings.values()):
            raise RuntimeError("mempool slot is already bound")
        page_handles = [(self.manager.rank, slot)]
        if self.manager.rank == 1:
            assert prompt_slot is not None
            page_handles.append((0, prompt_slot))
        attachment = KVRowBinding(
            req_pool_idx,
            slot,
            prompt_tokens,
            prompt_slot,
            prompt_tokens if self.manager.rank == 1 else start_position,
            tuple(
                (side, handle, self.manager.active_page_versions[side, handle])
                for side, handle in page_handles
            )
            if self.manager.page_tables
            else (),
        )
        self._bindings[req_pool_idx] = _Binding(attachment)
        self.row_slot[req_pool_idx] = slot
        self.row_prompt_len[req_pool_idx] = prompt_tokens
        self.row_prompt_slot[req_pool_idx] = (
            prompt_slot if prompt_slot is not None else -1
        )
        self.row_decode_len[req_pool_idx] = 0
        self._range_errors[req_pool_idx] = 0
        self._write_counts[:, slot].zero_()
        self._slot_totals[slot] = 0
        self._index_slot_totals[slot] = 0
        if self._index_counts is not None:
            self._index_counts[:, slot] = 0
        self._record_binding_update()
        return attachment

    def detach_row(self, binding: KVRowBinding) -> KVWriteReceipt:
        """Remove only this row mapping and return immutable local completion facts.

        The caller retains the receipt under its request attempt before returning
        native resources. It must prove native transfer safety and no future row
        submissions (and D drain on decode); this method never releases a BM slot.
        """
        if self.fault is not None:
            raise RuntimeError(f"mempool runtime fault: {self.fault}")
        if self._active is not None:
            raise RuntimeError("cannot change bindings during an open forward")
        req_pool_idx = binding.req_pool_idx
        self._row(req_pool_idx)
        if self._row_in_flight(req_pool_idx):
            raise RuntimeError("cannot detach an in-flight request row")
        self.assert_bound(req_pool_idx, binding)
        progress = self._bindings[req_pool_idx]
        receipt = KVWriteReceipt(
            binding,
            progress.submitted,
            progress.completed,
            progress.main_written,
            progress.index_written,
        )
        self.row_slot[req_pool_idx] = -1
        self.row_prompt_len[req_pool_idx] = -1
        self.row_prompt_slot[req_pool_idx] = -1
        self.row_decode_len[req_pool_idx] = 0
        del self._bindings[req_pool_idx]
        self.observation_forward.pop(req_pool_idx, None)
        self._record_binding_update()
        return receipt

    def assert_bound(self, req_pool_idx: int, binding: KVRowBinding | None) -> None:
        """Check a real row against the attachment saved for its approved attempt.

        The service projects req.kv.req_pool_idx and filters existing fake
        requests. Equal row/slot numbers alone cannot approve a new request.
        """
        self._row(req_pool_idx)
        progress = self._bindings.get(req_pool_idx)
        if binding is None or progress is None or progress.attachment is not binding:
            raise RuntimeError("request row has no matching mempool binding")
        self._check_page_versions(binding)

    def _check_page_versions(self, binding: KVRowBinding) -> None:
        for side, handle, version in binding.page_versions:
            if self.manager.active_page_versions.get((side, handle)) != version:
                self.fault = "stale request page table before forward"
                raise RuntimeError(self.fault)

    def prepare_forward(self, writes: Sequence[KVWriteExpectation]) -> None:
        """Save scheduler-approved host expectations for exactly one model call."""
        if self.fault is not None:
            raise RuntimeError(f"mempool runtime fault: {self.fault}")
        if self._prepared is not None or self._active is not None:
            raise RuntimeError("mempool forward preparation was not consumed")
        self._prepared = tuple(writes)

    @contextmanager
    def forward_scope(
        self, *, replay: bool = False, capture: bool = False
    ) -> Iterator[None]:
        """Bracket one eager/replay call or one capture warmup; fail closed."""
        try:
            if capture:
                if self._prepared is not None:
                    raise RuntimeError("capture cannot consume a scheduled request")
                writes: tuple[KVWriteExpectation, ...] = ()
            else:
                if self._prepared is None:
                    raise RuntimeError(
                        "mempool model call lacks scheduler expectations"
                    )
                writes = self._prepared
                self._prepared = None
            self.begin_forward(writes, replay=replay, capture=capture)
            if self.observation_trace is not None and not capture:
                from .observation_device import device_range

                ids = "-".join(
                    str(self.observation_forward[w.req_pool_idx]["forward_id"])
                    for w in writes
                )
                before = self.observer.clock()
                torch.npu.mstx.mark(f"mempool:clock:{ids}:{before}")
                self.observer.emit(
                    "clock_anchor",
                    forward_ids=ids,
                    before_ns=before,
                    after_ns=self.observer.clock(),
                )
                with device_range(f"forward:{ids}"):
                    yield
            else:
                yield
            self.end_forward()
            if replay and writes and not self._logged_replay:
                logger.info(
                    "mempool graph_replay real_requests=%s device=%s",
                    len(writes),
                    self.row_slot.device,
                )
                self._logged_replay = True
        except Exception as exc:
            self.fault = str(exc)
            # Do not publish completion or detach storage after a partial launch.
            raise

    def next_position(self, binding: KVRowBinding) -> int:
        """Return the next contiguous write coordinate for an approved attachment."""
        self.assert_bound(binding.req_pool_idx, binding)
        progress = self._bindings[binding.req_pool_idx]
        return progress.submitted + binding.write_start

    def begin_forward(
        self,
        writes: Sequence[KVWriteExpectation],
        *,
        replay: bool = False,
        capture: bool = False,
    ) -> None:
        """Validate expected progress outside capture, before eager/replay launch.

        These are per-request token counts, not per-layer counts. Capture uses
        no real expectations; replay uses this boundary even though Python
        layer hooks do not execute. Use the stream that submits the forward.
        """
        if self.fault is not None:
            raise RuntimeError(f"mempool runtime fault: {self.fault}")
        if self._active is not None:
            raise RuntimeError("a mempool forward is already open")
        if capture and (writes or replay):
            raise ValueError("capture requires an empty dummy forward")
        if capture and (self._bindings or self._pending):
            raise RuntimeError(
                "capture requires dummy storage without live bindings or pending work"
            )
        if replay and not self._capture_validated:
            raise RuntimeError("replay requires a validated captured write path")
        seen = set()
        for write in writes:
            self._row(write.req_pool_idx)
            if write.req_pool_idx in seen:
                raise ValueError("duplicate request in forward expectations")
            seen.add(write.req_pool_idx)
            positive_int("expected rows", write.rows)
            binding = self._bindings.get(write.req_pool_idx)
            if binding is None:
                raise RuntimeError("real forward request has no mempool binding")
            self._check_page_versions(binding.attachment)
            base = binding.attachment.write_start
            if write.start_position != base + binding.submitted:
                raise ValueError(
                    "expected writes must continue the submitted KV prefix"
                )
            limit = (
                (
                    len(self.manager.page_leases[1, binding.attachment.slot].pages)
                    * self.layout.page_size
                    if self.manager.page_tables
                    else self.layout.tokens
                )
                if self.manager.rank == 1
                else binding.attachment.prompt_tokens - base
            )
            if binding.submitted + write.rows > limit:
                raise ValueError("expected writes exceed request KV capacity")
        if self._binding_update is not None:
            self._wait_event(self._binding_update)
        if self.manager.rank == 0:
            for write in writes:
                self.row_write_start[write.req_pool_idx] = write.start_position
                self.row_write_end[write.req_pool_idx] = (
                    write.start_position + write.rows
                )
        if self._fetch_checks is not None:
            self._fetch_checks.zero_()
        if self._fetch_sources is not None:
            self._fetch_sources.zero_()
        if self.manager.rank == 1:
            for write in writes:
                if self._fetch is not None and write.rows != 1:
                    raise ValueError(
                        "sparse KV fetch requires one decode KV row per request"
                    )
                # Each layer's local write precedes fetch on this stream,
                # including D offset zero. Never infer this bound from output tokens.
                self.row_decode_len.narrow(0, write.req_pool_idx, 1).fill_(
                    self._bindings[write.req_pool_idx].submitted + write.rows
                )
        self._active = _Forward(tuple(writes), replay, capture)
        if self.observation_device is not None:
            self.observation_device.reset()

    def write_layer(
        self, layer_id: int, k: Tensor, k_rope: Tensor, forward_batch: Any
    ) -> None:
        """Infer rows and enqueue this layer's compact KV on the producer stream."""
        active = self._active
        if active is None or active.replay:
            raise RuntimeError("write_layer requires an eager/capture forward boundary")
        layer = layer_id - self.start_layer
        check_index("local layer", layer, self.layout.layers)
        if layer in active.layers:
            raise RuntimeError("duplicate mempool write for one forward layer")
        if k.ndim not in (2, 3) or k_rope.ndim not in (2, 3):
            raise ValueError("compact KV must have token/head/feature dimensions")
        values = torch.cat(
            [
                k.reshape(-1, self.layout.heads, k.shape[-1]),
                k_rope.reshape(-1, self.layout.heads, k_rope.shape[-1]),
            ],
            dim=-1,
        ).contiguous()
        req_ids, positions, native_valid = derive_kv_rows(
            values.shape[0],
            is_decode=forward_batch.forward_mode.is_decode(),
            max_context_len=self.max_context_len,
            req_pool_indices=forward_batch.req_pool_indices,
            seq_lens=forward_batch.seq_lens,
            out_cache_loc=forward_batch.out_cache_loc,
            extend_seq_lens=forward_batch.extend_seq_lens,
            extend_prefix_lens=forward_batch.extend_prefix_lens,
            extend_seq_lens_cpu=forward_batch.extend_seq_lens_cpu,
            global_num_token_non_padded_cpu=forward_batch.global_num_token_non_padded_cpu,
        )
        row_valid = (req_ids > 0) & (req_ids < self.row_slot.numel())
        safe_req = torch.where(row_valid, req_ids, 0)
        slots = self.row_slot[safe_req]
        prompt = self.row_prompt_len[safe_req]
        local_pos = positions - prompt if self.manager.rank == 1 else positions
        valid = native_valid & row_valid & (slots >= 0) & (local_pos >= 0)
        valid = valid & (
            local_pos < self.manager.view(self.manager.rank, 0).token_capacity
        )
        if self._fetch is not None:
            # A repeated or skipped device position must not count as a new
            # readable token when graph inputs disagree with host expectations.
            safe_write_slots = torch.where(valid, slots, 0)
            valid = (
                valid
                & (local_pos == self.row_decode_len[safe_req] - 1)
                & (local_pos == self._write_counts[layer, safe_write_slots])
            )
        if self.manager.rank == 0:
            valid = valid & (positions < prompt)
            if active.writes:
                in_range = (positions >= self.row_write_start[safe_req]) & (
                    positions < self.row_write_end[safe_req]
                )
                self._range_errors.scatter_add_(0, safe_req, (valid & ~in_range).long())
                valid = valid & in_range
        main_valid = valid
        if self.page_writer is not None:
            main_valid = main_valid & self.page_writer.mask(local_pos, prompt)
        if self._decode is not None:
            self._decode.prepare(layer, values, safe_req)
            if not self._decode.writer.owns_layer(layer):
                main_valid = torch.zeros_like(valid)
        if self.observer is not None:
            write_range = torch.npu.mstx.range_start(
                f"mempool:write_through:{layer_id}", torch.npu.current_stream()
            )
        self._writers[layer].write(
            values, slots=slots, positions=local_pos, valid=main_valid
        )
        if self.observer is not None:
            torch.npu.mstx.range_end(write_range)
        safe_slot = torch.where(valid, slots, 0)
        # Device counters catch silent invalid masks, including on graph replay.
        counted = valid if self._decode is not None else main_valid
        self._write_counts[layer].scatter_add_(0, safe_slot, counted.to(torch.int32))
        active.layers.add(layer)
        if active.writes and self.prefix_store is not None:
            self.prefix_store.capture_source(
                region="main",
                layer=layer,
                values=values,
                slots=slots,
                positions=local_pos,
                valid=valid,
            )
        if active.writes and layer_id in self._index_writers:
            active.index_layers.add(layer_id)
            owns_index = self.index_writer is None or self.index_writer.member >= 0
            if not owns_index and not (self.prefix_store and self.prefix_store.verify):
                return
            writer = self._index_writers[layer_id]
            native = self._index_pool.get_index_k_buffer(layer_id).reshape(
                -1, 1, writer.target.layout.dim
            )
            locations = forward_batch.out_cache_loc.reshape(-1).long()
            index_valid = valid & (locations >= 0) & (locations < native.shape[0])
            safe_locations = torch.where(index_valid, locations, 0)
            index_values = native.index_select(0, safe_locations).contiguous()
            owned_index = index_valid
            if self.index_writer is not None and owns_index:
                owned_index = owned_index & self.index_writer.mask(local_pos, prompt)
            assert self._index_counts is not None
            if owns_index:
                writer.write(
                    index_values, slots=slots, positions=local_pos, valid=owned_index
                )
                self._index_counts[writer.target.layer].scatter_add_(
                    0, safe_slot, owned_index.to(torch.int32)
                )
            active.retained.append(index_values)
            if self.prefix_store is not None:
                self.prefix_store.capture_source(
                    region="index",
                    layer=writer.target.layer,
                    values=index_values,
                    slots=slots,
                    positions=local_pos,
                    valid=index_valid,
                )

    def selected_kv_valid(
        self, layer_id: int, req_rows: Tensor, positions: Tensor
    ) -> Tensor:
        """Validate one selection before cache lookup; use this mask for all copies."""
        layer = self._fetch_layer(layer_id)
        active = self._active
        assert active is not None and self._fetch_checks is not None
        if layer in active.selection_layers:
            raise RuntimeError("duplicate KV selection for one forward layer")
        if (
            positions.ndim != 2
            or req_rows.ndim != 1
            or req_rows.numel() != positions.shape[0]
        ):
            raise ValueError("fetch requires one request row per top-k selection")
        if positions.shape[1] == 0:
            raise ValueError("fetch requires a positive top-k width")
        row_valid = (req_rows > 0) & (req_rows < self.row_slot.numel())
        safe_rows = torch.where(row_valid, req_rows, 0).long()
        bound = (
            row_valid
            & (self.row_slot[safe_rows] >= 0)
            & (self.row_prompt_slot[safe_rows] >= 0)
        )
        safe_slots = torch.where(bound, self.row_slot[safe_rows], 0).long()
        readable_decode = torch.minimum(
            self.row_decode_len[safe_rows], self._write_counts[layer, safe_slots]
        )
        valid = (
            bound[:, None]
            & (positions >= 0)
            & (positions < self.max_context_len)
            & (positions < (self.row_prompt_len[safe_rows] + readable_decode)[:, None])
        )
        invalid = bound[:, None] & (positions >= 0) & ~valid
        first = invalid.to(torch.int32).argmax(dim=1, keepdim=True)
        checks = torch.stack(
            (
                bound.long(),
                invalid.sum(1),
                torch.where(invalid.any(1), positions.gather(1, first).squeeze(1), -1),
            ),
            dim=1,
        )
        self._fetch_checks[layer].index_add_(
            0, safe_rows, torch.where(bound[:, None], checks, 0)
        )
        active.selection_layers.add(layer)
        return valid

    def _fetch_layer(self, layer_id: int) -> int:
        active = self._active
        layer = layer_id - self.start_layer
        check_index("local layer", layer, self.layout.layers)
        if self._fetch is None:
            raise RuntimeError("production mempool fetch is not enabled")
        if active is None or active.replay or layer not in active.layers:
            raise RuntimeError("fetch requires this layer's eager/captured KV write")
        return layer

    def fetch_selected_kv(
        self,
        layer_id: int,
        req_rows: Tensor,
        positions: Tensor,
        misses: Tensor,
        output: Tensor,
        *,
        cache_buffer: Tensor | None = None,
        cache_rows: Tensor | None = None,
        cache_valid: Tensor | None = None,
    ) -> None:
        """Fill only BM misses after selected_kv_valid and this layer's write.

        Submit on the miss stream after copy_ready; publish miss_done only after
        this call. Attention/refill must wait for that event and the hit stream.
        """
        layer = self._fetch_layer(layer_id)
        active = self._active
        assert self._fetch is not None and active is not None
        if layer not in active.selection_layers or layer in active.fetch_layers:
            raise RuntimeError(
                "fetch requires exactly one validated selection per layer"
            )
        safe = torch.where(
            (req_rows > 0) & (req_rows < self.row_slot.numel()), req_rows, 0
        ).long()
        current_mask = torch.zeros_like(misses)
        current_buffer = None
        if self._decode is not None:
            current_buffer = self._decode.current[layer]
            lengths = self.row_prompt_len + self.row_decode_len
            current_mask = (
                misses
                & (req_rows > 0)[:, None]
                & (positions == lengths[safe, None] - 1)
            )
            misses = misses & ~current_mask
        destination = torch.arange(
            positions.numel(), dtype=torch.int64, device=positions.device
        )
        zero_rows = torch.zeros_like(destination)
        extra_routes = (
            CopyIndices(
                cache_rows if cache_rows is not None else zero_rows,
                destination,
                cache_valid
                if cache_valid is not None
                else torch.zeros_like(destination, dtype=torch.bool),
            ),
            CopyIndices(
                safe[:, None].expand_as(positions).flatten(),
                destination,
                current_mask.flatten(),
            ),
        )
        copier = self._fetch.gather(
            layer,
            req_rows,
            positions,
            misses,
            output,
            self.row_prompt_slot,
            self.row_slot,
            self.row_prompt_len,
            self.row_decode_len,
            extra_sources=(cache_buffer, current_buffer),
            extra_routes=extra_routes,
        )
        if self.observation_device is not None:
            from .observation_device import device_range

            with device_range(f"observer_io:{layer_id}"):
                self.observation_device.materialized(
                    layer_id, safe, copier.source_regions, copier.transfer.valid
                )
                self.observation_device.io(
                    layer_id, req_rows, positions, misses, self.row_prompt_len
                )
        if self._fetch_sources is not None:
            # Captured alongside the fused materialization on the miss stream.
            # Read only after miss_done and the forward completion event. These
            # count submitted selected rows, not full logical/HBM pages.
            row_valid = (req_rows > 0) & (req_rows < self.row_slot.numel())
            safe_rows = torch.where(row_valid, req_rows, 0).long()
            prompt = self.row_prompt_len[safe_rows, None]
            valid = misses & row_valid[:, None] & (positions >= 0)
            counts = torch.stack(
                (
                    (valid & (positions < prompt)).sum(1),
                    (valid & (positions >= prompt)).sum(1),
                ),
                dim=1,
            )
            self._fetch_sources[layer].index_add_(0, safe_rows, counts)
        active.fetch_layers.add(layer)

    def completion_report(self, binding: KVRowBinding | None) -> dict[str, Any] | None:
        """Report validated decode completion before detaching its binding."""
        if not self.fetch_enabled:
            return None
        if self.fault is not None:
            raise RuntimeError(f"mempool runtime fault: {self.fault}")
        progress = None
        if binding is not None:
            row = binding.req_pool_idx
            self.assert_bound(row, binding)
            if self._row_in_flight(row):
                raise RuntimeError("cannot report an in-flight decode")
            progress = self._bindings[row]
        forwards = progress.forwards if progress else 0
        report: dict[str, Any] = {
            "status": "completed" if forwards else "zero_decode",
            "row": binding.req_pool_idx if binding else None,
            "prompt_slot": binding.prompt_slot if binding else None,
            "decode_slot": binding.slot if binding else None,
            "submitted_kv": progress.submitted if progress else 0,
            "written_kv": progress.completed if progress else 0,
            "forwards": forwards,
            "replay_forwards": progress.replay_forwards if progress else 0,
            "layers": self.layout.layers,
            "layer_checks": forwards * self.layout.layers,
            "source_evidence": self._fetch_sources is not None,
            "prompt_bm_rows": progress.prompt_bm_rows if progress else 0,
            "decode_bm_rows": progress.decode_bm_rows if progress else 0,
        }
        return report

    def end_forward(self) -> None:
        """Record completion after eager work or replay, without synchronizing."""
        active = self._active
        if active is None:
            raise RuntimeError("no mempool forward is open")
        if not active.replay and len(active.layers) != self.layout.layers:
            raise RuntimeError("mempool forward is missing local layer writes")
        if (
            active.writes
            and self._index_writers
            and active.index_layers != set(self._index_writers)
        ):
            raise RuntimeError("mempool forward is missing Index K producer writes")
        if (
            self._fetch is not None
            and not active.replay
            and len(active.fetch_layers) != self.layout.layers
        ):
            raise RuntimeError("mempool forward is missing local layer fetches")
        if active.capture:
            self._capture_validated = True
            self._active = None
            return
        rows = tuple(write.req_pool_idx for write in active.writes)
        for write in active.writes:
            binding = self._bindings[write.req_pool_idx]
            binding.submitted += write.rows
            slot = binding.attachment.slot
            self._index_slot_totals[slot] = (
                self.index_writer.rows(
                    binding.attachment.write_start,
                    binding.attachment.write_start + binding.submitted,
                    binding.attachment.prompt_tokens,
                )
                if self.index_writer is not None
                else binding.submitted
            )
            self._slot_totals[slot] = (
                self.page_writer.rows(
                    binding.attachment.write_start,
                    binding.attachment.write_start + binding.submitted,
                    binding.attachment.prompt_tokens,
                )
                if self.page_writer is not None
                else binding.submitted
            )
        totals = tuple(self._bindings[row].submitted for row in rows)
        snapshot = self._write_counts.clone()
        fetch = self._fetch_checks.clone() if self._fetch_checks is not None else None
        sources = (
            self._fetch_sources.clone() if self._fetch_sources is not None else None
        )
        index_snapshot = (
            self._index_counts.clone() if self._index_counts is not None else None
        )
        range_errors = self._range_errors.clone() if self.manager.rank == 0 else None
        observation: Any = None
        if self.observer is not None:
            observation = dict(
                rows={row: dict(self.observation_forward[row]) for row in rows},
                pages=self.observation_device.snapshot(
                    torch.tensor(rows, dtype=torch.int64, device=self.row_slot.device)
                )
                if self.observation_device is not None
                else None,
                materialization=self.observation_device.materialization_snapshot(
                    torch.tensor(rows, dtype=torch.int64, device=self.row_slot.device)
                )
                if self.observation_device is not None
                else None,
                source_rows=sources,
            )
            for values in observation["rows"].values():
                self.observer.emit("forward_submitted", replay=active.replay, **values)
        event = self._event_factory()
        event.record()
        self._pending.append(
            _Completion(
                event,
                snapshot,
                rows,
                totals,
                tuple(self._slot_totals),
                active.replay,
                fetch,
                sources,
                index_snapshot,
                tuple(active.retained),
                range_errors,
                tuple(self._index_slot_totals),
                observation,
            )
        )
        self._active = None

    def poll_completed(self) -> list[int]:
        """Validate completed per-layer counts and publish local progress facts."""
        completed = []
        while self._pending and self._pending[0].event.query():
            item = self._pending[0]
            if item.observation is not None:
                self.collect_observation(item.observation)
            if item.range_errors is not None and bool(item.range_errors.cpu().any()):
                self.fault = "mempool write range/counts differ from expected rows"
                raise RuntimeError(self.fault)
            counts = item.snapshot.cpu().tolist()
            if any(tuple(layer) != item.slot_totals for layer in counts):
                self.fault = "mempool valid write counts differ from expected rows"
                raise RuntimeError(self.fault)
            index_counts = (
                item.index_snapshot.cpu().tolist()
                if item.index_snapshot is not None
                else None
            )
            if index_counts is not None and any(
                tuple(layer) != item.index_totals for layer in index_counts
            ):
                self.fault = "mempool Index K write counts differ from expected rows"
                raise RuntimeError(self.fault)
            fetch_counts = item.fetch.cpu().tolist() if item.fetch is not None else None
            source_counts = (
                item.sources.cpu().sum(0).tolist() if item.sources is not None else None
            )
            if fetch_counts is not None:
                for layer, samples in enumerate(fetch_counts):
                    for row, sample in enumerate(samples):
                        checks, invalid, position = sample
                        if checks != int(row in item.rows) or invalid:
                            self.fault = (
                                f"mempool KV fetch invalid selection layer={layer + self.start_layer} "
                                f"row={row} position={position} invalid_kv={invalid} "
                                f"checks={checks} expected={int(row in item.rows)}"
                            )
                            raise RuntimeError(self.fault)
            for row, total in zip(item.rows, item.totals):
                progress = self._bindings[row]
                if self.prefix_store is not None:
                    self.prefix_store.verify_source(
                        slot=progress.attachment.slot,
                        tokens=total,
                        start=progress.attachment.write_start,
                    )
                progress.completed = total
                progress.main_written = counts[0][progress.attachment.slot]
                if index_counts is not None:
                    progress.index_written = index_counts[0][progress.attachment.slot]
                progress.forwards += 1
                progress.replay_forwards += int(item.replay)
                if source_counts is not None:
                    progress.prompt_bm_rows += source_counts[row][0]
                    progress.decode_bm_rows += source_counts[row][1]
                completed.append(row)
            self._pending.popleft()
        return completed

    def collect_observation(self, snapshot: dict[str, Any]) -> None:
        """Use the existing completed event; never read mutable graph outputs."""
        import time

        started = time.monotonic_ns()
        data = snapshot["pages"]
        counts: Any
        equal: Any
        counts, equal = (
            (data[0].cpu().tolist(), data[1].cpu().tolist())
            if data is not None
            else (None, None)
        )
        source_rows = (
            snapshot["source_rows"].cpu().tolist()
            if snapshot.get("source_rows") is not None
            else None
        )
        materialization = snapshot.get("materialization")
        cache_validation = (
            materialization[2].cpu().tolist() if materialization is not None else None
        )
        material_rows = (
            materialization[0].cpu().tolist() if materialization is not None else None
        )
        cache_changes = (
            materialization[1].cpu().tolist() if materialization is not None else None
        )
        for row_index, (row, identity) in enumerate(snapshot["rows"].items()):
            owned_rows = (
                self.page_writer.rows(
                    identity["write_start"],
                    identity["write_start"] + identity["write_rows"],
                    identity["prompt_tokens"],
                )
                if self.page_writer is not None
                else identity["write_rows"]
            )
            layer_bytes = [owned_rows * self.layout.row_bytes] * self.layout.layers
            if self._decode is not None:
                writer = self._decode.writer
                layer_bytes = [
                    owned_rows * self.layout.row_bytes // writer.feature_part[1]
                    if writer.owns_layer(layer)
                    else 0
                    for layer in range(self.layout.layers)
                ]
            self.observer.emit(
                "forward_complete_observed",
                **identity,
                write_through_bytes_by_layer=layer_bytes,
                index_write_bytes_by_layer={
                    producer: (
                        self.index_writer.rows(
                            identity["write_start"],
                            identity["write_start"] + identity["write_rows"],
                            identity["prompt_tokens"],
                        )
                        if self.index_writer is not None
                        else identity["write_rows"]
                    )
                    * self.manager.layout.prompt_index.row_bytes
                    for producer in self._index_writers
                }
                if self.manager.layout.prompt_index is not None
                else {},
                completion_evidence="existing device event queried complete",
            )
            if counts is None or identity["step"] % self.observation_sample_every:
                continue
            if source_rows is not None:
                actual = [sum(layer[row][i] for layer in source_rows) for i in (0, 1)]
                observed = [
                    sum(page[i] for layer in counts for page in layer[row_index])
                    for i in (3, 4)
                ]
                self.observer.emit(
                    "read_byte_reconciliation",
                    **identity,
                    actual_source_rows=actual,
                    page_source_rows=observed,
                    actual_source_bytes=[v * self.layout.row_bytes for v in actual],
                    passed=actual == observed,
                )
            for layer, layer_rows in enumerate(counts):
                layer_id = layer + self.start_layer
                producer = max(p for p in self.observation_producers if p <= layer_id)
                pages = []
                for logical, values in enumerate(layer_rows[row_index]):
                    selected, hit, miss, p_io, d_io, d_early = values
                    if not selected:
                        continue
                    source = []
                    d_page = (
                        logical
                        - identity["prompt_tokens"] // self.observation_page_size
                    )
                    for kind, number, pool_rank, slot, offset in (
                        ("P_remote", p_io, 0, identity["prompt_slot"], logical),
                        ("D_local", d_early, 1, identity["decode_slot"], d_page - 1),
                        ("D_local", d_io - d_early, 1, identity["decode_slot"], d_page),
                    ):
                        if number:
                            view = self.manager.view(pool_rank, layer)
                            session = self.manager.session
                            members = (
                                session.region_members(pool_rank) if session else ()
                            )
                            owner = (
                                session.region_owner(pool_rank)
                                if session
                                else pool_rank
                            )
                            source.append(
                                dict(
                                    source=kind,
                                    rows=number,
                                    nbytes=number * self.layout.row_bytes,
                                    region=f"{session.identity if session else 'pair'}:alive_main:{owner}",
                                    owner_participant=owner,
                                    replica=members[0] if members else 0,
                                    sharing_group=list(members),
                                    source_host=identity["prefill_host"]
                                    if pool_rank == 0
                                    else identity["decode_host"],
                                    source_numa=session.prefix_nodes[
                                        next(
                                            i
                                            for i, g in enumerate(session.main_groups)
                                            if members[0] in g
                                        )
                                    ]
                                    if session and session.main_groups
                                    else self.manager.numa_node,
                                    physical_page=view.physical_page(slot, offset),
                                )
                            )
                    hashes = self.observation_hashes[identity["rid"]]
                    pages.append(
                        dict(
                            logical_page=logical,
                            content_page=hashes[logical]
                            if logical < len(hashes)
                            else f"{identity['rid']}:generated:{logical}",
                            selected_count=selected,
                            hit_count=hit,
                            miss_count=miss,
                            io_miss_count=p_io + d_io,
                            current_device_supply_count=miss - p_io - d_io,
                            status="miss" if miss else "hit",
                            has_hit=bool(hit),
                            sources=source,
                        )
                    )
                if (
                    material_rows is not None
                    and cache_changes is not None
                    and cache_validation is not None
                ):
                    names = [
                        "P_alive",
                        "D_alive",
                        *[f"prefix_{i}" for i in range(8)],
                        "HBM_cache",
                        "HBM_current",
                    ]
                    source_payload = dict(zip(names, material_rows[layer][row_index]))
                    evicted, refilled, mapped = cache_changes[layer][row_index]
                    self.observer.emit(
                        "materialization",
                        **identity,
                        layer_id=layer_id,
                        source_rows=source_payload,
                        source_bytes={
                            k: v * self.layout.row_bytes
                            for k, v in source_payload.items()
                        },
                        current_dram_rows=0,
                        refill_dram_bytes=0,
                        refill_hbm_bytes=refilled * self.layout.row_bytes,
                        cache_refill_checked_rows=cache_validation[layer][row_index][0],
                        cache_refill_errors=cache_validation[layer][row_index][1],
                        slot_map_checked_rows=cache_validation[layer][row_index][2],
                        slot_map_missing=cache_validation[layer][row_index][3],
                        slot_map_stale=cache_validation[layer][row_index][4],
                        evicted_entries=evicted,
                        refilled_rows=refilled,
                        slot_map_updates=mapped,
                        copy_launches=dict(materialize=1, refill=1, slot_map=1),
                        materialize_ns=None,
                        miss_exposed_upper_ns=None,
                        materialize_wait_ns=None,
                        source_time_ns=None,
                        source_time_reason="fused",
                        timing_reason="device trace required; no host submit timing",
                    )
                self.observer.emit(
                    "layer_pages",
                    **identity,
                    layer_id=layer_id,
                    sharing_group=list(self.manager.session.region_members(1))
                    if self.manager.session
                    else [],
                    producer_layer=producer,
                    ordered_selection_equal=bool(equal[layer][row_index]),
                    pages=pages,
                    selected_count=sum(p["selected_count"] for p in pages),
                    miss_count=sum(p["miss_count"] for p in pages),
                    io_miss_count=sum(p["io_miss_count"] for p in pages),
                )
        self.observer.emit("observer_collect", elapsed_ns=time.monotonic_ns() - started)
        self.observer.flush()

    def written_tokens(self, req_pool_idx: int) -> int:
        """Return locally completed KV rows, distinct from generated tokens."""
        return self._bindings[req_pool_idx].completed

    def main_written_tokens(self, req_pool_idx: int) -> int:
        return self._bindings[req_pool_idx].main_written

    def index_written_tokens(self, req_pool_idx: int) -> int:
        return self._bindings[req_pool_idx].index_written

    def written_range(self, req_pool_idx: int) -> tuple[int, int]:
        binding = self._bindings[req_pool_idx]
        start = binding.attachment.write_start
        return start, start + binding.completed

    def writes_done(self, req_pool_idx: int) -> bool:
        """Check local completion only; remote visibility/drain are caller duties."""
        binding = self._bindings[req_pool_idx]
        return (
            not self._row_in_flight(req_pool_idx)
            and binding.completed == binding.submitted
        )

    def prompt_ready(self, req_pool_idx: int) -> bool:
        """Report a complete local prompt; the control layer proves D visibility."""
        if self.manager.rank != 0:
            raise RuntimeError("prompt readiness belongs to P")
        binding = self._bindings[req_pool_idx]
        return (
            self.writes_done(req_pool_idx)
            and binding.attachment.write_start == 0
            and binding.completed == binding.attachment.prompt_tokens
        )


def model_forward_scope(
    backend: Any, *, replay: bool = False, capture: bool = False
) -> Any:
    """Return a runtime scope when this attention backend owns mempool storage."""
    runtime = getattr(backend, "mempool_runtime", None)
    return (
        runtime.forward_scope(replay=replay, capture=capture)
        if runtime is not None
        else nullcontext()
    )


def initialize_for_model_runner(model_runner: Any) -> None:
    """Map BM and attach one runtime before model graph capture, on P and D."""
    import importlib

    args = model_runner.server_args
    if args.device != "npu":
        raise ValueError("mempool requires device='npu'")
    backend = model_runner.attn_backend
    from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
        SparseKVOffloadMode,
    )

    config = getattr(model_runner, "mempool_config", None)
    if config is None:
        raise ValueError("mempool configuration must be validated before KV allocation")
    mode = getattr(backend, "sparse_kv_offload_mode", None)
    if mode is None or not mode.uses_mempool_bm:
        raise ValueError("mempool runtime requires a mempool startup mode")
    if not hasattr(backend, "attach_mempool_runtime"):
        raise ValueError("mempool requires the Ascend MLA attention backend")
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import (
        CONFIG_ENV,
        PRODUCERS,
        RUN_ENV,
        RouterConfig,
    )

    router = (
        RouterConfig.read(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else None
    )
    prefix_pages = router.prefix_pages if router else 0
    native_pool = model_runner.token_to_kv_pool
    if prefix_pages and (
        tuple(native_pool.indexer_layer_ids) != tuple(PRODUCERS)
        or native_pool.index_head_dim != 128
        or native_pool.index_k_scale_buffer is not None
        or native_pool.get_index_k_buffer(PRODUCERS[0]).dtype != torch.bfloat16
        or router is None
        or config.page_size != router.page_size
    ):
        raise ValueError(
            "prefix requires the configured BF16 Index K producer geometry"
        )
    layout = config.make_mla_layout(
        num_layers=model_runner.layer_info.end_layer
        - model_runner.layer_info.start_layer,
        kv_lora_rank=model_runner.model_config.kv_lora_rank,
        qk_rope_head_dim=model_runner.model_config.qk_rope_head_dim,
        prefix_pages=prefix_pages,
        indexer_layers=len(PRODUCERS) if prefix_pages else 0,
        index_head_dim=128 if prefix_pages else 0,
    )
    sparse = getattr(backend, "sparse_kv_manager", None)
    host_buffers = getattr(sparse, "host_kv_buffer", [])
    logger.info(
        "[MEMPOOL_INIT] CONFIG pid=%d role=%s tp_rank=%d device_id=%d "
        "start_layer=%d req_pool_rows=%d max_context_len=%d "
        "existing_host_shm_layers=%d existing_host_shm_bytes=%d layout=%s",
        os.getpid(),
        args.disaggregation_mode,
        model_runner.ps.tp_rank,
        model_runner.gpu_id,
        model_runner.layer_info.start_layer,
        model_runner.req_to_token_pool.req_to_token.shape[0],
        model_runner.model_config.context_len,
        len(host_buffers),
        sum(tensor.numel() * tensor.element_size() for tensor in host_buffers),
        json.dumps(layout.signature(), sort_keys=True),
    )
    with startup_stage("mf.import"):
        mf = importlib.import_module("memfabric_hybrid")
    if diagnostics_enabled():
        # MF's INFO output exposes HAL allocation/export/import/map boundaries.
        # This is process-wide, including subsequent TransferEngine diagnostics.
        log_ret = mf.set_log_level(1)
        with startup_stage("npu.current_device"):
            current_device = torch.npu.current_device()
        logger.info(
            "[MEMPOOL_INIT] ENV pid=%d mf_module=%s mf_set_log_level_ret=%s "
            "current_device=%d settings=%s",
            os.getpid(),
            getattr(mf, "__file__", "unknown"),
            log_ret,
            current_device,
            json.dumps(
                {
                    name: os.environ.get(name)
                    for name in (
                        "ASCEND_RT_VISIBLE_DEVICES",
                        "ASCEND_VISIBLE_DEVICES",
                        "SGLANG_SET_CPU_AFFINITY",
                        "SGLANG_NUMA_BIND_V2",
                        "SGLANG_AUTO_NUMA_BIND",
                        "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE",
                        "PYTORCH_NPU_ALLOC_CONF",
                        "SGLANG_NPU_USE_MULTI_STREAM",
                        "TASK_QUEUE_ENABLE",
                    )
                },
                sort_keys=True,
            ),
        )
    with startup_stage("mf.initialize"):
        ret = mf.initialize()
        if ret != 0:
            raise RuntimeError(f"MemFabric initialization failed: {ret}")
    # BM and TransferEngine share process-wide MF. No per-request/global MF
    # uninitialize, atexit pool destroy, or fault-path close is permitted here.
    from urllib.parse import urlsplit

    from .sharing_plan import HostTopology, local_online_nodes

    address = urlsplit(config.nic)
    host = address.hostname or ""
    plan = config.numa.plan(
        hosts=(HostTopology(host, tuple(range(16)), local_online_nodes()),),
        tp_size=16,
        role=args.disaggregation_mode,
        instance_id=os.environ.get(RUN_ENV, "mempool"),
        kv_identity=args.model_path,
        layout=layout,
        base_port=config.base_port,
        nic_port=address.port or 0,
        pool_id=config.pool_id,
        prefill_shared=bool(router and router.prefill_shared),
        prefill_groups=router.session_groups if router else (),
        decode_shared=bool(router and router.decode_shared),
        prefill_index_shared=bool(router and router.prefill_index_shared),
        prefix_shared=bool(router and router.prefix_shared),
    )
    manager = MempoolKVManager.initialize_session(
        session=plan.session(model_runner.ps.tp_rank),
        store_host=config.prefill_host,
        nic_host=host,
        device_id=model_runner.gpu_id,
        timeout=config.timeout,
    )
    with startup_stage("runtime.allocate", bm_rank=manager.rank):
        device = str(model_runner.req_to_token_pool.req_to_token.device)
        session = manager.session
        page_writer = (
            PageWriter(
                session.region_members(0).index(session.tp_rank),
                len(session.region_members(0)),
                config.page_size,
                router.prefill_write_assignment,
            )
            if session is not None
            and session.prefill_shared
            and manager.rank == 0
            and router is not None
            else None
        )
        prefix_group = None
        index_writer = (
            PageWriter(
                router.prefill_index_writers.index(session.tp_rank)
                if session.tp_rank in router.prefill_index_writers
                else -1,
                len(router.prefill_index_writers),
                config.page_size,
            )
            if router
            and router.prefill_index_shared
            and session is not None
            and manager.rank == 0
            else None
        )
        if router and router.match_prefix and manager.rank == 0:
            from sglang.srt.distributed import get_tp_group

            prefix_group = get_tp_group()
        copy_executor = None
        if prefix_pages and manager.rank == 0 and router and router.pool_copy:
            from functools import partial

            from .pool_copy import PoolCopyExecutor, device_thread_setup

            copy_executor = PoolCopyExecutor(
                manager.pool_transfer(),
                timeout=router.operation_timeout,
                setup=partial(
                    device_thread_setup,
                    device,
                    manager.numa_node,
                    router.pool_copy_bind_cpu,
                ),
            )
        prefix_store = (
            PrefixStore(
                manager,
                page_size=config.page_size,
                device=device,
                verify=bool(router and router.verify_prefix),
                replica=model_runner.ps.tp_rank,
                readers=prefix_group.world_size if prefix_group is not None else 1,
                read_assignment=router.read_assignment if router else "round_robin",
                all_gather=prefix_group.all_gather_into_tensor
                if prefix_group is not None
                else None,
                page_writer=page_writer,
                index_writer=index_writer,
                admit_mode=router.prefix_admit_mode if router else "owners",
                admit_rank=router.prefix_admit_rank if router else 0,
                start_segment=router.prefix_start_segment if router else None,
                copy_executor=copy_executor,
            )
            if prefix_pages and manager.rank == 0
            else None
        )
        if router is not None:
            manager.enable_page_tables(model_runner.device)
        runtime = MempoolRuntime(
            manager,
            req_pool_rows=model_runner.req_to_token_pool.req_to_token.shape[0],
            max_context_len=model_runner.model_config.context_len,
            start_layer=model_runner.layer_info.start_layer,
            device=device,
            source_evidence=bool(os.environ.get("SGLANG_NPU_MEMPOOL_ROUTER_CONFIG")),
            fetch_enabled=mode is SparseKVOffloadMode.PD_DECODE_MEMPOOL,
            index_pool=native_pool if prefix_store is not None else None,
            prefix_store=prefix_store,
            page_writer=page_writer,
            index_writer=index_writer,
            decode_writer=DecodeWriter(
                session.region_members(1).index(session.tp_rank),
                len(session.region_members(1)),
                router.decode_write_assignment,
            )
            if router
            and router.decode_shared
            and session is not None
            and manager.rank == 1
            else None,
        )
    runtime.config = config
    if router is not None and router.observe:
        from .observation import create_observation
        from .observation_device import DeviceTrace, PageObservation

        runtime.observer = create_observation(
            router,
            os.environ[RUN_ENV],
            args.disaggregation_mode,
            model_runner.ps.tp_rank,
        )
        runtime.observation_sample_every = router.observe_sample_every
        runtime.observation_page_size = config.page_size
        runtime.observation_config = router
        runtime.observation_hot_cache_tokens = (
            sparse.sparse_context_len if sparse else 0
        )
        runtime.observation_producers = tuple(native_pool.indexer_layer_ids)
        if runtime._fetch is not None:
            runtime._fetch.observe = router.observe_trace
            runtime._fetch.count_sources = router.observe
            runtime._fetch.start_layer = runtime.start_layer
        if sparse is not None and runtime.fetch_enabled:
            runtime.observation_device = PageObservation(
                layout.decode.layers,
                runtime.row_slot.numel(),
                runtime.max_context_len,
                config.page_size,
                sparse.sparse_context_len,
                runtime.observation_producers,
                runtime.start_layer,
                device,
            )
        assert runtime.observer is not None
        runtime.observer.emit(
            "run",
            page_size_tokens=config.page_size,
            hot_cache_tokens=sparse.sparse_context_len if sparse else 0,
            top_k=getattr(model_runner.model_config.hf_config, "index_topk", None),
            replacement_policy="replace_selected",
            reader_numa=manager.numa_node,
            sharing_group=list(session.region_members(manager.rank))
            if session
            else [model_runner.ps.tp_rank],
            sample_every=router.observe_sample_every,
            layers=layout.prompt.layers,
            producer_layers=runtime.observation_producers,
            tp_size=model_runner.ps.tp_size,
            torch_version=torch.__version__,
            sdk_build_id=router.pool_copy_sdk_build_id,
            geometry=layout.signature(),
            topology=plan.report(),
            trace_enabled=router.observe_trace,
            profiler="MSTX device ranges" if router.observe_trace else "disabled",
        )
        runtime.observer.flush()
        if router.observe_trace:
            runtime.observation_trace = DeviceTrace(
                runtime.observer.path.parent
                / f"trace-{args.disaggregation_mode}-{model_runner.ps.tp_rank}"
            )
            runtime.observation_trace.install_hooks(model_runner.model)
            from sglang.srt.runtime_context import get_parallel

            parallel = get_parallel()
            runtime.observation_trace.install_collectives(
                [
                    parallel.tp_group,
                    parallel.attn_tp_group,
                    parallel.moe_tp_group,
                ]
            )
        if prefix_store is not None:
            if prefix_group is not None:
                prefix_store.all_gather = prefix_group.all_gather_into_tensor
            prefix_store.observer = runtime.observer
            if copy_executor is not None:
                copy_executor.observer = runtime.observer
                from .pool_copy import BMTransfer

                if isinstance(copy_executor.transfer, BMTransfer):
                    copy_executor.transfer.observer = runtime.observer
    seen = set()
    for candidate in (backend, model_runner.decode_attn_backend):
        if candidate is not None and id(candidate) not in seen:
            with startup_stage("runtime.attach", backend=type(candidate).__name__):
                candidate.attach_mempool_runtime(runtime)
            seen.add(id(candidate))
    logger.info(
        "[MEMPOOL_INIT] READY pid=%d role=%s tp_rank=%d device_id=%d; "
        "BM/runtime initialized, PD control handshake still pending",
        os.getpid(),
        args.disaggregation_mode,
        model_runner.ps.tp_rank,
        model_runner.gpu_id,
    )
