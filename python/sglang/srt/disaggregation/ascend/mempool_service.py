"""Connect persistent mempool control to SGLang requests and native lifetimes."""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sglang.srt.hardware_backend.npu.mempool.runtime import (
    KVReadbackError,
    KVRowBinding,
    KVWriteExpectation,
    KVWriteReceipt,
    MempoolRuntime,
)

from .mempool_control import MempoolPDControl, MempoolRequestSnapshot
from .mempool_protocol import MempoolMessage, PoolDescriptor, PoolPeer, RequestIdentity
from .mempool_tick import MempoolTPTick, RequestObservation

logger = logging.getLogger(__name__)


@dataclass
class _Request:
    """Retain local request/cleanup facts, never a second protocol phase or slot."""

    req: Any
    deadline: float
    attempt: str
    identity: RequestIdentity | None = None
    binding: KVRowBinding | None = None
    receipt: KVWriteReceipt | None = None
    transfer_ready: bool = False
    release: bool = False
    handoff: bool = False
    is_insert: bool = True
    drained: bool = False
    native_freed: bool = False
    cancel: bool = False
    native_wait: Any = None
    cancelled_at: float | None = None


class MempoolPDService:
    """Project Req fields, hold native cleanup, and supply facts to the TP tick.

    Storage and protocol have independent lifetimes. On P a native release
    detaches only the row; the control allocation survives until D's exact DONE.
    On D, callbacks register release, and tick drains the whole side before free.
    """

    def __init__(
        self,
        runtime: MempoolRuntime,
        control: MempoolPDControl,
        *,
        gather: Callable[[Any], Sequence[Any]],
        send: Callable[[str, MempoolMessage], None],
        reply_to: str,
        endpoint: str | None = None,
        bootstrap_timeout: float = 600.0,
        native_release: Callable[[Any, bool], None],
        drain_host: Callable[[], None],
        synchronize: Callable[[], None],
        cancel_request: Callable[[Any], None],
        clock: Callable[[], float] = time.monotonic,
        verify_peer: Callable[[PoolPeer], None] = lambda peer: None,
        timeout: float = 120.0,
    ) -> None:
        """Inject device, native scheduler and network boundaries for CPU checking."""
        self.runtime = runtime
        self.control = control
        self.clock = clock
        self.bootstrap_timeout = bootstrap_timeout
        self._native_release = native_release
        self._drain_host = drain_host
        self._synchronize = synchronize
        self._cancel_request = cancel_request
        self._requests: dict[int, _Request] = {}
        self.fault: str | None = None
        self.scheduler: Any = None
        self.kv_manager: Any = None
        self.timeout = timeout
        self._tick_started: float | None = None
        self.tick = MempoolTPTick(
            control,
            gather=gather,
            send=send,
            reply_to=reply_to,
            endpoint=endpoint,
            effect=self._effect,
            clock=clock,
            verify_peer=verify_peer,
            timeout=timeout,
        )

    @classmethod
    def from_scheduler(cls, scheduler: Any) -> MempoolPDService:
        """Attach existing Ascend transport after BM/Graph initialization has finished."""
        import threading

        import torch

        from sglang.srt.environ import envs
        from sglang.srt.utils.network import NetworkAddress

        runtime = scheduler.tp_worker.model_runner.attn_backend.mempool_runtime
        if runtime is None or runtime.config is None:
            raise RuntimeError(
                "mempool mappings/runtime must be ready before PD control"
            )
        config = runtime.config
        role = scheduler.server_args.disaggregation_mode
        queue = (
            scheduler.disagg_prefill_bootstrap_queue
            if role == "prefill"
            else scheduler.disagg_decode_prealloc_queue
        )
        manager = queue.kv_manager
        # Finalize logical Index K metadata after physical buffer registration,
        # but before any receiver can publish KVArgs or admit a real request.
        pool = scheduler.token_to_kv_pool_allocator.get_kvcache()
        transfer_layout = manager.configure_mempool_transfer(pool)
        layout = runtime.manager.layout
        peer = PoolPeer(
            secrets.token_hex(32),
            role,
            scheduler.tp_worker.model_runner.ps.tp_rank,
            16,
            1,
            config.pool_id,
            PoolDescriptor(
                layout.prompt.layers,
                16,
                layout.prompt.tokens,
                layout.decode.tokens,
                layout.prompt.heads,
                layout.prompt.dim,
                layout.prompt.dtype,
                layout.contribution_bytes(0),
                layout.contribution_bytes(1),
                layout.rank_stride_bytes,
            ),
            transfer_kind="index_k_only" if transfer_layout is not None else "full",
            transport_session=manager.get_session_id(),
            transfer_layout=transfer_layout,
        )
        control = MempoolPDControl(peer)
        device = str(runtime.row_slot.device)
        runtime.manager.probe_peer(
            torch.tensor(
                list(peer.session.encode("ascii")), dtype=torch.uint8, device=device
            )
        )

        def gather(value: Any) -> list[Any]:
            """Use the entire side's TP CPU group, including empty scheduler ticks."""
            result: list[Any] = [None] * 16
            torch.distributed.all_gather_object(
                result, value, group=scheduler.tp_cpu_group
            )
            return result

        service = cls(
            runtime,
            control,
            gather=gather,
            send=manager.send_mempool_message,
            reply_to=NetworkAddress(manager.local_ip, manager.rank_port).to_tcp(),
            bootstrap_timeout=envs.SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT.get(),
            native_release=lambda req, insert: service._free_native(req, insert),
            drain_host=lambda: service._flush_scheduler(),
            synchronize=torch.npu.synchronize,
            cancel_request=lambda req: service._cancel_scheduler_request(req),
            verify_peer=lambda remote: runtime.manager.verify_local_probe(
                remote.session.encode("ascii"), device
            ),
            timeout=config.timeout,
        )
        service.scheduler = scheduler
        service.kv_manager = manager
        manager.attach_mempool_control(control)
        # A failed collective cannot report through that same collective. This
        # watchdog bounds a stuck tick without treating termination as drain.
        threading.Thread(
            target=service._watch_tick, daemon=True, name="mempool-tick-watchdog"
        ).start()
        logger.info(
            "mempool mapping_ready role=%s rank=%s session=%s P_bytes=%s D_bytes=%s stride=%s",
            role,
            peer.tp_rank,
            peer.session,
            layout.contribution_bytes(0),
            layout.contribution_bytes(1),
            layout.rank_stride_bytes,
        )
        return service

    def _watch_tick(self) -> None:
        """Terminate a hung local tick; never synthesize release or destroy BM."""
        import os
        import signal

        while True:
            time.sleep(min(1.0, self.timeout / 4))
            started = self._tick_started
            if started is not None and self.clock() - started > self.timeout:
                logger.critical(
                    "mempool TP tick watchdog expired; stop both P/D before restart"
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return

    @staticmethod
    def is_fake(req: Any) -> bool:
        """Use SGLang's existing fake-bootstrap marker; never infer fake from a row."""
        return bool(req.bootstrap_host == "2.2.2.2")

    def track(self, req: Any) -> None:
        """Retain the original bootstrap deadline before any mempool wait."""
        if self.is_fake(req):
            return
        room = req.bootstrap_room
        if type(room) is not int or room < 0:
            raise ValueError("real mempool request requires a bootstrap room")
        existing = self._requests.get(room)
        if existing is not None:
            if existing.req is not req:
                raise ValueError(
                    "bootstrap room already belongs to a different request"
                )
            return
        if self.control.local.role == "decode" and any(
            r.identity.room == room for r in self.control.snapshot().requests
        ):
            raise ValueError(
                "mempool demo requires a fresh bootstrap room; rebootstrap is unsupported"
            )
        record = _Request(
            req, self.clock() + self.bootstrap_timeout, secrets.token_hex(16)
        )
        if len(req.origin_input_ids) > self.runtime.manager.layout.prompt.tokens:
            record.cancel = True
        if (
            self.control.local.role == "decode"
            and req.sampling_params.max_new_tokens
            > self.runtime.manager.layout.decode.tokens
        ):
            record.cancel = True
        self._requests[room] = record

    def tracks(self, req: Any) -> bool:
        """Distinguish this exact admitted request from fake or grammar-pending work."""
        record = self._requests.get(req.bootstrap_room)
        return record is not None and record.req is req

    def _record(self, req: Any) -> _Request:
        """Require identity of the tracked Req, not just its reusable row number."""
        record = self._requests.get(req.bootstrap_room)
        if record is None or record.req is not req:
            raise RuntimeError("real request has no matching mempool service record")
        return record

    def _protocol(self, record: _Request) -> MempoolRequestSnapshot | None:
        """Read the one control owner and retain only the request's exact identity."""
        candidates = [
            r
            for r in self.control.snapshot().requests
            if r.identity.room == record.req.bootstrap_room
            and (record.identity is None or r.identity == record.identity)
        ]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise RuntimeError("ambiguous mempool request attempt")
        result = candidates[0]
        record.identity = result.identity
        return result

    def can_prefill(self, req: Any) -> bool:
        """Read the tick's approval before sender initialization has side effects."""
        if self.is_fake(req):
            return True
        record = self._record(req)
        state = self._protocol(record)
        return not record.cancel and state is not None and state.phase == "PREFILLING"

    def can_preallocate(self, req: Any) -> bool:
        """Admit native D allocations only after the complete binding is approved."""
        if self.is_fake(req):
            return True
        record = self._record(req)
        state = self._protocol(record)
        return (
            not record.cancel and state is not None and state.phase == "WAITING_READY"
        )

    def transfer_complete(self, req: Any) -> bool:
        """Observe raw native success first, then read the joint decode approval."""
        if self.is_fake(req):
            return True
        record = self._record(req)
        if not record.transfer_ready:
            logger.info(
                "mempool native_transfer_ready role=decode rank=%s room=%s",
                self.control.local.tp_rank,
                req.bootstrap_room,
            )
        record.transfer_ready = True
        state = self._protocol(record)
        return state is not None and state.phase == "DECODING" and not record.cancel

    def prepare_batch(self, batch: Any) -> None:
        """Install approved row mappings and expect only this batch's actual writes."""
        if batch.forward_mode.is_idle() or batch.forward_mode.is_prebuilt():
            return
        writes = []
        for index, req in enumerate(batch.reqs):
            if self.is_fake(req):
                continue
            record = self._record(req)
            state = self._protocol(record)
            required_phase = (
                "DECODING" if self.control.local.role == "decode" else "PREFILLING"
            )
            if (
                state is None
                or state.phase != required_phase
                or record.cancel
                or record.release
            ):
                raise RuntimeError(
                    "request entered a model batch without mempool approval"
                )
            row = req.kv.req_pool_idx
            if record.binding is None:
                lease = (
                    state.d_slot
                    if self.control.local.role == "decode"
                    else state.p_slot
                )
                if lease is None or not state.owns_slot:
                    raise RuntimeError("approved request has no owned mempool slot")
                record.binding = self.runtime.bind(
                    row,
                    slot=lease.slot,
                    prompt_tokens=state.prompt_limit,
                    prompt_slot=state.p_slot.slot if state.p_slot is not None else None,
                )
            self.runtime.assert_bound(row, record.binding)
            if batch.forward_mode.is_decode():
                start, rows = self.runtime.next_position(record.binding), 1
            else:
                start, rows = batch.prefix_lens[index], batch.extend_lens[index]
            writes.append(KVWriteExpectation(row, start, rows))
        self.runtime.prepare_forward(writes)

    def defer_release(
        self, req: Any, is_insert: bool = True, *, handoff: bool = False
    ) -> bool:
        """Register a native free exactly once; completion callbacks never drain."""
        if self.is_fake(req):
            return False
        record = self._record(req)
        if record.native_freed:
            return True
        record.release = True
        record.is_insert = is_insert
        if handoff and not record.handoff:
            logger.info(
                "mempool native_handoff role=prefill rank=%s room=%s",
                self.control.local.tp_rank,
                req.bootstrap_room,
            )
        record.handoff |= handoff
        return True

    def abort_matching(self, rid: str, *, abort_all: bool = False) -> None:
        """Queue client cancellation; the next TP plan authorizes transitions."""
        for record in self._requests.values():
            if abort_all or record.req.rid.startswith(rid):
                record.cancel = True

    def _observations(self) -> list[RequestObservation]:
        """Consume device completion and project live or detached write facts."""
        self._poll_completed()
        facts = []
        for room, record in self._requests.items():
            state = self._protocol(record)
            if (
                state is not None
                and state.phase in ("CANCELLED", "RELEASED")
                and not record.native_freed
            ):
                record.cancel = True
            if record.native_wait is not None and self.kv_manager is not None:
                if self.control.local.role == "prefill":
                    record.handoff = bool(
                        self.kv_manager.mempool_prefill_transfer_drained(room)
                    )
                else:
                    receiver = record.native_wait.kv_receiver
                    infos = receiver.bootstrap_infos
                    record.transfer_ready = bool(
                        infos
                        and self.kv_manager.is_abort_release_safe(room, len(infos))
                    )
                if (
                    record.cancelled_at is not None
                    and self.clock() - record.cancelled_at > self.timeout
                ):
                    safe = (
                        record.handoff
                        if self.control.local.role == "prefill"
                        else record.transfer_ready
                    )
                    if not safe:
                        raise TimeoutError(
                            "native transfer drain unconfirmed; retain storage and stop both sides"
                        )
            row = record.binding.req_pool_idx if record.binding else None
            done = row is None or self.runtime.writes_done(row)
            prompt_ready = (
                record.receipt is not None
                and record.receipt.completed == len(record.req.origin_input_ids)
            )
            if row is not None and self.control.local.role == "prefill":
                prompt_ready = self.runtime.prompt_ready(row)
            facts.append(
                RequestObservation(
                    room,
                    len(record.req.origin_input_ids),
                    int(record.req.sampling_params.max_new_tokens),
                    record.deadline,
                    record.attempt,
                    transfer_ready=record.transfer_ready,
                    prompt_ready=prompt_ready,
                    writes_done=done and (not record.cancel or record.drained),
                    native_release=(
                        record.release
                        and record.handoff
                        and done
                        and (not record.cancel or record.drained)
                        and not record.native_freed
                    ),
                    release=record.release,
                    drained=record.drained
                    and done
                    and (
                        record.transfer_ready
                        if self.control.local.role == "decode"
                        else record.handoff
                    ),
                    host_drained=record.drained,
                    cancel=record.cancel
                    or (state is not None and state.phase == "CANCELLING"),
                    native_freed=record.native_freed,
                )
            )
        return facts

    def _poll_completed(self) -> None:
        """Attach rank and request identity to numerical failures before fault consensus."""
        try:
            self.runtime.poll_completed()
        except KVReadbackError as exc:
            matches = [
                {
                    "room": room,
                    "rid": record.req.rid,
                    "attempt": record.identity.attempt if record.identity else "NONE",
                    "prompt_slot": record.binding.prompt_slot,
                    "decode_slot": record.binding.slot,
                }
                for room, record in self._requests.items()
                if record.binding is not None and record.binding.req_pool_idx == exc.row
            ]
            logger.error(
                "mempool KV readback failed role=%s rank=%s error=%s requests=%s",
                self.control.local.role,
                self.control.local.tp_rank,
                exc,
                json.dumps(matches, sort_keys=True),
            )
            raise

    def advance(self) -> None:
        """Run one complete scheduler tick, preserving faults for same-side consensus."""
        self._tick_started = self.clock()
        facts: list[RequestObservation] = []
        try:
            facts = self._observations()
        except Exception as exc:
            self.fault = str(exc)
        try:
            if (
                self.scheduler is not None
                and self.control.local.role == "decode"
                and self.tick.endpoint is None
            ):
                config = self.runtime.config
                if config is None:
                    raise RuntimeError("mempool startup settings missing")
                self.tick.endpoint = self.kv_manager.discover_mempool_peer(
                    config.prefill_host,
                    config.bootstrap_port,
                    self.control.local.tp_rank,
                )
        except Exception as exc:
            self.fault = str(exc)
        try:
            self.tick.advance(facts, fault=self.fault or self.runtime.fault)
            self._retire_local_records()
        finally:
            self._tick_started = None

    def _retire_local_records(self) -> None:
        """Bound retained Req references after both native and protocol retirement."""
        for room, record in list(self._requests.items()):
            state = self._protocol(record)
            if record.native_freed and (
                state is None
                and record.cancel
                or state is not None
                and state.phase in ("CANCELLED", "CLOSED", "RELEASED")
            ):
                if self.scheduler is None or not self._request_in_queues(record.req):
                    del self._requests[room]

    def _request_in_queues(self, req: Any) -> bool:
        """Keep exact attempts available until native queues have retired the Req."""
        scheduler = self.scheduler
        if req in scheduler.waiting_queue or req in scheduler.running_batch.reqs:
            return True
        if self.control.local.role == "prefill":
            return (
                req in scheduler.disagg_prefill_bootstrap_queue.queue
                or req in scheduler.disagg_prefill_inflight_queue
            )
        return any(
            dr.req is req
            for queue in (
                scheduler.disagg_decode_prealloc_queue,
                scheduler.disagg_decode_transfer_queue,
            )
            for dr in queue.queue
        )

    def _effect(self, kind: str, room: int) -> None:
        """Execute scheduler effects only from an approved, preflighted TP plan."""
        if kind == "drain":
            started = self.clock()
            self._drain_host()
            self._synchronize()
            self._poll_completed()
            for record in self._requests.values():
                if record.release or record.cancel:
                    record.drained = True
            logger.info(
                "mempool drain role=%s seconds=%.6f",
                self.control.local.role,
                self.clock() - started,
            )
            return
        record = self._requests[room]
        if kind in ("cancel", "reject"):
            if record.native_freed:
                return
            record.cancel = True
            record.release = True
            self._cancel_request(record.req)
            if record.cancelled_at is None:
                record.cancelled_at = self.clock()
        elif kind == "native_release" and not record.native_freed:
            readback = self.runtime.readback_report(record.binding)
            if readback is not None:
                readback["cancelled"] = record.cancel
                logger.info(
                    "mempool readback_result role=decode rank=%s room=%s attempt=%s data=%s",
                    self.control.local.tp_rank,
                    room,
                    record.identity.attempt if record.identity else "NONE",
                    json.dumps(readback, sort_keys=True),
                )
            if record.binding is not None:
                record.receipt = self.runtime.detach_row(record.binding)
                record.binding = None
                logger.info(
                    "mempool row_detach role=%s rank=%s room=%s attempt=%s row=%s slot=%s completed=%s",
                    self.control.local.role,
                    self.control.local.tp_rank,
                    room,
                    record.identity.attempt if record.identity else "NONE",
                    record.receipt.binding.req_pool_idx,
                    record.receipt.binding.slot,
                    record.receipt.completed,
                )
            self._native_release(record.req, record.is_insert)
            record.native_freed = True
            logger.info(
                "mempool native_free role=%s rank=%s room=%s attempt=%s",
                self.control.local.role,
                self.control.local.tp_rank,
                room,
                record.identity.attempt if record.identity else "NONE",
            )

    def has_pending_work(self) -> bool:
        """Keep scheduler idle/sleep checks aware of persistent ownership and cleanup."""
        return any(not r.native_freed for r in self._requests.values()) or any(
            r.phase not in ("CANCELLED", "RELEASED", "CLOSED")
            for r in self.control.snapshot().requests
        )

    def bootstrap_failed(self, req: Any) -> None:
        """Cancel an unallocated D request without inventing a remote drain."""
        if self.is_fake(req):
            return
        record = self._record(req)
        if req.kv.req_pool_idx is not None:
            raise RuntimeError(
                "bootstrap failure unexpectedly holds native KV destinations"
            )
        record.cancel = True
        record.release = True
        record.transfer_ready = True

    def native_failure(self, req: Any, decode_request: Any = None) -> bool:
        """Hold failed native destinations until actual transfer drain, without a free timeout."""
        if self.is_fake(req):
            return False
        record = self._record(req)
        record.cancel = True
        record.release = True
        record.is_insert = False
        record.cancelled_at = record.cancelled_at or self.clock()
        if decode_request is not None:
            record.native_wait = decode_request
            decode_request.kv_receiver.abort()
        else:
            record.native_wait = req.disagg_kv_sender
            req.disagg_kv_sender.abort()
        return True

    def _free_native(self, req: Any, is_insert: bool) -> None:
        """Run original cleanup once, outside any allocator free-group callback."""
        from sglang.srt.disaggregation.prefill import maybe_release_metadata_buffer
        from sglang.srt.mem_cache.common import release_kv_cache

        scheduler = self.scheduler
        record = self._record(req)
        if req.kv.req_pool_idx is not None:
            prepare_release = getattr(
                scheduler.model_worker, "prepare_for_kv_cache_release", None
            )
            if callable(prepare_release):
                prepare_release(req)
            release_kv_cache(req, scheduler.tree_cache, is_insert=is_insert)
        if self.control.local.role == "prefill":
            maybe_release_metadata_buffer(
                req, scheduler.req_to_metadata_buffer_idx_allocator
            )
            if req.disagg_kv_sender is not None:
                req.disagg_kv_sender.clear()
            record.native_wait = None
        elif record.native_wait is not None:
            dr = record.native_wait
            queue = scheduler.disagg_decode_transfer_queue
            if queue.enable_staging and queue.staging_handler.is_staging_room(
                req.bootstrap_room
            ):
                queue.staging_handler.unregister_decode_req(req.bootstrap_room)
            dr.kv_receiver.clear()
            dr.kv_receiver = None
            if dr.metadata_buffer_index >= 0:
                queue.metadata_buffers.bootstrap_room[dr.metadata_buffer_index] = 0
                scheduler.req_to_metadata_buffer_idx_allocator.free(
                    dr.metadata_buffer_index
                )
                dr.metadata_buffer_index = -1
            self.kv_manager.clear_deferred_abort_state(req.bootstrap_room)
            record.native_wait = None

    def _cancel_scheduler_request(self, req: Any) -> None:
        """Stop this real request and preserve native transfer resources until drain."""
        from sglang.srt.disaggregation.utils import prepare_abort

        scheduler = self.scheduler
        record = self._record(req)
        if not req.finished():
            prepare_abort(
                req, "Ascend mempool request cancelled or bootstrap timed out"
            )
            scheduler.output_streamer.stream_output([req], req.return_logprob)
        if self.control.local.role == "prefill":
            sender = req.disagg_kv_sender
            if sender is not None and not record.handoff:
                self.native_failure(req)
            else:
                record.handoff = True
        elif not record.transfer_ready:
            for queue in (
                scheduler.disagg_decode_prealloc_queue,
                scheduler.disagg_decode_transfer_queue,
            ):
                for dr in queue.queue:
                    if dr.req is req:
                        if req.kv.req_pool_idx is None:
                            # No destination has been published, so no native writer exists.
                            record.transfer_ready = True
                        else:
                            self.native_failure(req, dr)
                        break
        scheduler.waiting_queue[:] = [
            r for r in scheduler.waiting_queue if r is not req
        ]

    def _flush_scheduler(self) -> None:
        """Stop submissions and retire overlap host work before the device fence."""
        scheduler = self.scheduler
        queue = getattr(scheduler, "result_queue", None)
        while queue:
            batch, result = queue.popleft()
            scheduler.launch_batch_sample_if_needed(result, batch)
            scheduler.process_batch_result(batch, result)
        # The normal overlap loop must not pop a result that was already consumed.
        scheduler.last_batch = None
        scheduler.cur_batch_for_debug = None
        scheduler.running_batch.filter_batch()
        if scheduler.chunked_req is not None and scheduler.chunked_req.finished():
            scheduler.clear_pending_chunk_send(scheduler.chunked_req)
            scheduler.chunked_req = None
            scheduler._pending_chunked_abort_req = None

    def cancel_for_retraction(self, batch: Any) -> None:
        """Turn unsupported automatic rebootstrap into explicit cancellation."""
        for req in batch.reqs:
            if not self.is_fake(req):
                self._record(req).cancel = True
        # Keep resources until the next uniform tick; no retract allocator runs.
        batch.reqs = []
