"""Adapt model forwards to BM writes without owning PD protocol transitions."""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Protocol

import torch
from torch import Tensor

from .config import MempoolConfig
from .diagnostics import diagnostics_enabled, startup_stage
from .layout import check_index, positive_int
from .manager import MempoolKVManager
from .offload import MempoolKVOffload
from .rows import derive_kv_rows

logger = logging.getLogger(__name__)


class WriteEvent(Protocol):
    """Represent completion on a device submission stream."""

    def record(self) -> None:
        """Record after preceding binding updates or KV write operations."""
        ...

    def query(self) -> bool:
        """Return whether all recorded work has completed."""
        ...


@dataclass(frozen=True)
class KVWriteExpectation:
    """Describe real rows processed this forward, in full-sequence coordinates."""

    req_pool_idx: int
    start_position: int
    rows: int


@dataclass(frozen=True, eq=False)
class KVRowBinding:
    """Identify one local row attachment, even if its coordinates are later reused.

    Keep the object returned by bind with the approved request attempt. Object
    identity distinguishes attachments without importing PD sessions/generations.
    This is not slot ownership: only control may acquire or release a slot.
    """

    req_pool_idx: int
    slot: int
    prompt_tokens: int


@dataclass(frozen=True)
class KVWriteReceipt:
    """Retain completed local progress after the request row has been detached."""

    binding: KVRowBinding
    submitted: int
    completed: int


@dataclass
class _Binding:
    """Keep local submission/completion progress independent of output tokens."""

    attachment: KVRowBinding
    submitted: int = 0
    completed: int = 0


@dataclass
class _Forward:
    """Track one Python forward boundary, including graph replay submissions."""

    writes: tuple[KVWriteExpectation, ...]
    replay: bool
    capture: bool
    layers: set[int] = field(default_factory=set)


@dataclass
class _Completion:
    """Retain an ordered count snapshot until its device event completes."""

    event: WriteEvent
    snapshot: Tensor
    rows: tuple[int, ...]
    totals: tuple[int, ...]
    slot_totals: tuple[int, ...]


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
        kernels: Optional[Sequence[Any]] = None,
        event_factory: Optional[Callable[[], WriteEvent]] = None,
        wait_event: Optional[Callable[[WriteEvent], None]] = None,
    ) -> None:
        """Allocate stable bindings/counters after BM mappings are ready."""
        if mlapo_enabled:
            raise ValueError("mempool does not support MLAPO")
        positive_int("req_pool_rows", req_pool_rows)
        positive_int("max_context_len", max_context_len)
        if type(start_layer) is not int or start_layer < 0:
            raise ValueError("start_layer must be a nonnegative integer")
        self.config: Optional[MempoolConfig] = None
        self.manager = manager
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
        self._write_counts = torch.zeros(
            (self.layout.layers, self.layout.slots), dtype=torch.int32, device=device
        )
        self._writers = [
            MempoolKVOffload(
                manager.view(manager.rank, layer),
                block_dim=block_dim,
                kernel=kernels[layer] if kernels is not None else None,
            )
            for layer in range(self.layout.layers)
        ]
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
        self.fault: Optional[str] = None
        self._prepared: Optional[tuple[KVWriteExpectation, ...]] = None
        self._bindings: dict[int, _Binding] = {}
        self._slot_totals = [0] * self.layout.slots
        self._pending: deque[_Completion] = deque()
        self._active: Optional[_Forward] = None
        self._capture_validated = False
        self._logged_replay = False
        self._binding_update: Optional[WriteEvent] = None
        self._record_binding_update()

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

    def bind(self, req_pool_idx: int, *, slot: int, prompt_tokens: int) -> KVRowBinding:
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
        if req_pool_idx in self._bindings or self._row_in_flight(req_pool_idx):
            raise RuntimeError("request row is already bound or in-flight")
        if any(binding.attachment.slot == slot for binding in self._bindings.values()):
            raise RuntimeError("mempool slot is already bound")
        attachment = KVRowBinding(req_pool_idx, slot, prompt_tokens)
        self._bindings[req_pool_idx] = _Binding(attachment)
        self.row_slot[req_pool_idx] = slot
        self.row_prompt_len[req_pool_idx] = prompt_tokens
        self._write_counts[:, slot].zero_()
        self._slot_totals[slot] = 0
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
        receipt = KVWriteReceipt(binding, progress.submitted, progress.completed)
        self.row_slot[req_pool_idx] = -1
        self.row_prompt_len[req_pool_idx] = -1
        del self._bindings[req_pool_idx]
        self._record_binding_update()
        return receipt

    def assert_bound(self, req_pool_idx: int, binding: Optional[KVRowBinding]) -> None:
        """Check a real row against the attachment saved for its approved attempt.

        The service projects req.kv.req_pool_idx and filters existing fake
        requests. Equal row/slot numbers alone cannot approve a new request.
        """
        self._row(req_pool_idx)
        progress = self._bindings.get(req_pool_idx)
        if binding is None or progress is None or progress.attachment is not binding:
            raise RuntimeError("request row has no matching mempool binding")

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
        return progress.submitted + (
            binding.prompt_tokens if self.manager.rank == 1 else 0
        )

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
            base = binding.attachment.prompt_tokens if self.manager.rank == 1 else 0
            if write.start_position != base + binding.submitted:
                raise ValueError(
                    "expected writes must continue the submitted KV prefix"
                )
            limit = (
                self.layout.tokens
                if self.manager.rank == 1
                else binding.attachment.prompt_tokens
            )
            if binding.submitted + write.rows > limit:
                raise ValueError("expected writes exceed request KV capacity")
        if self._binding_update is not None:
            self._wait_event(self._binding_update)
        self._active = _Forward(tuple(writes), replay, capture)

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
        valid = valid & (local_pos < self.layout.tokens)
        if self.manager.rank == 0:
            valid = valid & (positions < prompt)
        self._writers[layer].write(
            values, slots=slots, positions=local_pos, valid=valid
        )
        safe_slot = torch.where(valid, slots, 0)
        # Device counters catch silent invalid masks, including on graph replay.
        self._write_counts[layer].scatter_add_(0, safe_slot, valid.to(torch.int32))
        active.layers.add(layer)

    def end_forward(self) -> None:
        """Record completion after eager work or replay, without synchronizing."""
        active = self._active
        if active is None:
            raise RuntimeError("no mempool forward is open")
        if not active.replay and len(active.layers) != self.layout.layers:
            raise RuntimeError("mempool forward is missing local layer writes")
        if active.capture:
            self._capture_validated = True
            self._active = None
            return
        rows = tuple(write.req_pool_idx for write in active.writes)
        for write in active.writes:
            binding = self._bindings[write.req_pool_idx]
            binding.submitted += write.rows
            self._slot_totals[binding.attachment.slot] = binding.submitted
        totals = tuple(self._bindings[row].submitted for row in rows)
        snapshot = self._write_counts.clone()
        event = self._event_factory()
        event.record()
        self._pending.append(
            _Completion(event, snapshot, rows, totals, tuple(self._slot_totals))
        )
        self._active = None

    def poll_completed(self) -> list[int]:
        """Validate completed per-layer counts and publish local progress facts."""
        completed = []
        while self._pending and self._pending[0].event.query():
            item = self._pending[0]
            counts = item.snapshot.cpu().tolist()
            if any(tuple(layer) != item.slot_totals for layer in counts):
                raise RuntimeError(
                    "mempool valid write counts differ from expected rows"
                )
            for row, total in zip(item.rows, item.totals):
                self._bindings[row].completed = total
                completed.append(row)
            self._pending.popleft()
        return completed

    def written_tokens(self, req_pool_idx: int) -> int:
        """Return locally completed KV rows, distinct from generated tokens."""
        return self._bindings[req_pool_idx].completed

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
    from sglang.srt.environ import envs
    from sglang.srt.hardware_backend.npu.attention.mla_preprocess import (
        is_mla_preprocess_enabled,
    )

    config = MempoolConfig.from_server_args(
        args,
        sparse_enabled=envs.SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD.get(),
        mla=model_runner.use_mla_backend,
        dtype=str(model_runner.kv_cache_dtype).removeprefix("torch."),
        mlapo=is_mla_preprocess_enabled(),
    )
    if not hasattr(backend, "attach_mempool_runtime"):
        raise ValueError("mempool requires the Ascend MLA attention backend")
    layout = config.make_mla_layout(
        num_layers=model_runner.layer_info.end_layer
        - model_runner.layer_info.start_layer,
        kv_lora_rank=model_runner.model_config.kv_lora_rank,
        qk_rope_head_dim=model_runner.model_config.qk_rope_head_dim,
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
    manager = MempoolKVManager.initialize_rank_pair(
        layout=layout,
        role=args.disaggregation_mode,
        tp_rank=model_runner.ps.tp_rank,
        device_id=model_runner.gpu_id,
        store_host=config.prefill_host,
        base_port=config.base_port,
        nic_url=config.nic_for_rank(model_runner.ps.tp_rank),
        pool_id=config.pool_id,
        timeout=config.timeout,
    )
    with startup_stage("runtime.allocate", bm_rank=manager.rank):
        runtime = MempoolRuntime(
            manager,
            req_pool_rows=model_runner.req_to_token_pool.req_to_token.shape[0],
            max_context_len=model_runner.model_config.context_len,
            start_layer=model_runner.layer_info.start_layer,
            device=str(model_runner.req_to_token_pool.req_to_token.device),
        )
    runtime.config = config
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
