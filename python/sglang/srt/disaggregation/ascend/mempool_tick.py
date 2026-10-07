"""Advance one side's sixteen persistent controls in a fixed collective order."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import msgspec
from msgspec.structs import replace

from .mempool_control import MempoolPDControl, MempoolRequestSnapshot
from .mempool_protocol import MempoolMessage, MessageType, PoolPeer, RequestIdentity

logger = logging.getLogger(__name__)


class RequestObservation(msgspec.Struct, frozen=True):
    """Describe local service facts, leaving protocol phase and slots in control."""

    room: int
    prompt_tokens: int
    decode_tokens: int
    deadline: float
    attempt: str = ""
    transfer_ready: bool = False
    prompt_ready: bool = False
    writes_done: bool = False
    native_release: bool = False
    release: bool = False
    drained: bool = False
    host_drained: bool = False
    cancel: bool = False
    native_freed: bool = False


class _RequestState(msgspec.Struct, frozen=True):
    """Compare logical protocol facts without transferring pair-local proofs."""

    room: int
    attempt: str
    phase: str
    d_slot: tuple[int, int]
    p_slot: tuple[int, int] | None
    owns_slot: bool
    prompt_limit: int
    decode_limit: int
    prompt_written: int | None
    transfer_ready: bool
    writes_pending: bool
    binding_confirmed: bool
    pending_done: bool
    release_ack: bool

    @property
    def retired(self) -> bool:
        return self.phase in ("CANCELLED", "CLOSED", "RELEASED")

    @classmethod
    def from_request(cls, record: MempoolRequestSnapshot) -> _RequestState:
        return cls(
            record.identity.room,
            record.identity.attempt,
            record.phase,
            (record.d_slot.slot, record.d_slot.generation),
            (record.p_slot.slot, record.p_slot.generation) if record.p_slot else None,
            record.owns_slot,
            record.prompt_limit,
            record.decode_limit,
            record.prompt_written,
            record.transfer_ready,
            record.writes_pending,
            record.binding_confirmed,
            record.pending_done is not None,
            record.release_ack is not None,
        )


class _MessageState(msgspec.Struct, frozen=True):
    """Agree on metadata after preparing the original message locally."""

    key: tuple[Any, ...]
    prompt_tokens: int | None
    decode_tokens: int | None
    p_slot: tuple[int, int] | None
    d_slot: tuple[int, int] | None


class _Observation(msgspec.Struct, frozen=True):
    """Serialize one worker's durable observations for the same-side TP group."""

    requests: tuple[_RequestState, ...]
    cleanup: tuple[_RequestState, ...]
    available_slots: frozenset[int]
    peer_ready: bool
    facts: tuple[RequestObservation, ...]
    messages: tuple[_MessageState, ...]
    now: float
    fault: str | None
    record_capacity: int
    prepared: tuple[_Preparation, ...] = ()


class _Action(msgspec.Struct, frozen=True):
    """Identify an agreed operation without transporting another pair's proofs."""

    kind: str
    room: int = -1
    attempt: str = ""
    slot: int = -1
    message_key: tuple[Any, ...] = ()

    @property
    def preparation_key(self) -> tuple[Any, ...]:
        """D admission prepares a fresh room before the leader chooses attempt.

        Acquisition validates against a representative free slot. The common
        planner assigns distinct slots from the same observed free set.
        """
        attempt = "" if self.kind == "acquire_decode" else self.attempt
        return self.kind, self.room, attempt, self.message_key


class _Preparation(msgspec.Struct, frozen=True):
    """Carry a candidate's local validation and admission cost in the first gather."""

    key: tuple[Any, ...]
    record_claim: int = 0
    error: str | None = None


class MempoolTPTick:
    """Prepare locally, agree on a plan, then commit and gather its status.

    gather is the existing TP CPU group's all-gather boundary. Network threads
    only enqueue; even an empty advance participates in two collectives.
    Effects run on the scheduler thread and must not release protocol slots.
    """

    def __init__(
        self,
        control: MempoolPDControl,
        *,
        gather: Callable[[Any], Sequence[Any]],
        send: Callable[[str, MempoolMessage], None],
        reply_to: str,
        endpoint: str | None = None,
        effect: Callable[[str, int], None] = lambda kind, room: None,
        clock: Callable[[], float] = time.monotonic,
        verify_peer: Callable[[PoolPeer], None] = lambda peer: None,
        timeout: float = 120.0,
    ) -> None:
        """Attach system boundaries; all mutable protocol state stays in control."""
        self.control = control
        self.gather = gather
        self.send = send
        self.reply_to = reply_to
        self.endpoint = endpoint
        self.effect = effect
        self.clock = clock
        self.verify_peer = verify_peer
        self.timeout = timeout
        self._last_peer_seen = self.clock()
        self._last_sent = float("-inf")
        self._messages: dict[tuple[Any, ...], MempoolMessage] = {}

    def advance(
        self, facts: Sequence[RequestObservation], fault: str | None = None
    ) -> None:
        """Complete one TP decision; any worker failure prevents all sends."""
        start = self.clock()
        try:
            for message in self.control.drain_inbox():
                if message.kind == MessageType.HEARTBEAT:
                    if self.control.peer is not None:
                        self.control.apply(message)
                        self._last_peer_seen = start
                    continue
                key = self._message_key(message)
                previous = self._messages.get(key)
                if previous is not None and previous != message:
                    raise ValueError("conflicting duplicate mempool message")
                self._messages[key] = message
            if start - self._last_peer_seen > self.timeout:
                raise TimeoutError(
                    "mempool peer startup/heartbeat timed out; stop both PD sides"
                )
            if len(self._messages) > 4096:
                raise RuntimeError(
                    "unmatched mempool messages exceeded the bounded inbox"
                )
        except Exception as exc:
            fault = str(exc)
        # Include terminal attempts only while their service facts still need
        # local cleanup. Historical network messages are checked by control;
        # their retained records need not be identical across TP ranks.
        peer = self.control.peer
        include = []
        if peer is not None:
            p, d = (
                (self.control.local, peer)
                if self.control.local.role == "prefill"
                else (peer, self.control.local)
            )
            include = [
                RequestIdentity(f.room, f.attempt, p.session, d.session)
                for f in facts
                if f.attempt
            ]
        snapshot = self.control.active_snapshot(include)
        records = {(r.identity.room, r.identity.attempt): r for r in snapshot.requests}
        states = tuple(
            _RequestState.from_request(records[key]) for key in sorted(records)
        )
        observation = _Observation(
            tuple(state for state in states if not state.retired),
            tuple(state for state in states if state.retired),
            snapshot.available_slots,
            peer is not None,
            tuple(facts),
            tuple(
                _MessageState(
                    key,
                    m.prompt_tokens,
                    m.decode_tokens,
                    (m.p_slot.slot, m.p_slot.generation) if m.p_slot else None,
                    (m.d_slot.slot, m.d_slot.generation) if m.d_slot else None,
                )
                for key, m in self._messages.items()
            ),
            start,
            fault or snapshot.protocol_fault,
            self.control.admission_capacity(),
        )
        facts_by_room = {fact.room: fact for fact in facts}
        try:
            if observation.fault is None:
                observation = replace(
                    observation,
                    prepared=self._prepare_candidates(
                        observation=observation,
                        facts_by_room=facts_by_room,
                        records=records,
                    ),
                )
        except Exception as exc:
            observation = replace(observation, fault=str(exc))
        observations = self.gather(observation)
        error = None
        outbox: list[MempoolMessage] = []
        try:
            if any(o.fault for o in observations):
                raise RuntimeError("mempool worker reported a fault")
            plan = self._approve_plan(self._plan(observations), observations)
            self.control.assert_healthy()
            outbox = self._commit(
                plan=plan,
                facts_by_room=facts_by_room,
                records=records,
                control=self.control,
            )
        except Exception as exc:
            error = str(exc)
        statuses = self.gather(error)
        if any(statuses):
            reason = next(e for e in statuses if e)
            self.control.fail_protocol(reason)
            raise RuntimeError(f"mempool TP tick failed: {reason}")
        for message in outbox:
            if self.endpoint is None:
                raise RuntimeError("mempool peer endpoint is missing")
            self.send(self.endpoint, message)
        if self.endpoint is not None and start - self._last_sent >= 1.0:
            peer = self.control.peer
            if peer is not None:
                self.send(
                    self.endpoint,
                    MempoolMessage(
                        MessageType.HEARTBEAT,
                        peer=self.control.local,
                        receiver_session=peer.session,
                    ),
                )
            elif self.control.local.role == "decode":
                self.send(self.endpoint, self.control.begin_handshake(self.reply_to))
            self._last_sent = start
        log_tick = logger.info if plan else logger.debug
        log_tick(
            "mempool tick role=%s rank=%s seconds=%.6f actions=%s",
            self.control.local.role,
            self.control.local.tp_rank,
            self.clock() - start,
            len(plan),
        )

    def _prepare_candidates(
        self,
        observation: _Observation,
        facts_by_room: Mapping[int, RequestObservation],
        records: dict[tuple[int, str], MempoolRequestSnapshot],
    ) -> tuple[_Preparation, ...]:
        """Prepare every control transition the common planner can choose.

        Errors belong to their candidate, not the entire tick: another rank may
        not have received this message yet, or cancellation may take precedence.
        No service effects, inbox consumption or ownership changes happen here.
        """
        candidates = [
            _Action("message", key[1], key[2], message_key=key)
            for key in self._messages
        ]
        free_slot = min(observation.available_slots, default=-1)
        for state in observation.requests:
            room, attempt = state.room, state.attempt
            fact = facts_by_room.get(room)
            if fact is not None and fact.attempt != attempt:
                fact = None
            if state.phase not in ("WAITING_RELEASE_ACK", "CANCELLING", "DRAINING"):
                # Cancellation on any rank can select this on every rank.
                candidates.append(_Action("cancel", room, attempt))
            if self.control.local.role == "prefill":
                if state.phase == "WAITING_ACQUIRE" and free_slot >= 0:
                    candidates.append(
                        _Action("acquire_prefill", room, attempt, free_slot)
                    )
                elif state.phase == "BOUND":
                    candidates.append(_Action("start_prefill", room, attempt))
                elif state.phase == "PREFILLING" and fact and fact.prompt_ready:
                    candidates.append(_Action("ready", room, attempt))
                elif (
                    state.phase == "CANCELLING"
                    and state.writes_pending
                    and fact
                    and fact.writes_done
                ):
                    candidates.append(_Action("writes_done", room, attempt))
            elif state.phase == "WAITING_READY":
                if not state.transfer_ready and fact and fact.transfer_ready:
                    candidates.append(_Action("transfer", room, attempt))
                elif state.transfer_ready and state.prompt_written is not None:
                    candidates.append(_Action("start_decode", room, attempt))
            elif (
                state.phase in ("DECODING", "CANCELLING", "DRAINING")
                and fact
                and fact.drained
            ):
                # A different rank may be the first to report release=True.
                candidates.append(_Action("release", room, attempt))
        if (
            self.control.local.role == "decode"
            and observation.peer_ready
            and free_slot >= 0
        ):
            occupied = {
                state.room for state in (*observation.requests, *observation.cleanup)
            }
            candidates.extend(
                _Action("acquire_decode", f.room, f.attempt, free_slot)
                for f in facts_by_room.values()
                if f.room not in occupied and not f.native_freed
            )
        prepared = []
        for action in candidates:
            try:
                record = records.get((action.room, action.attempt))
                request = record.identity if record else None
                if action.kind == "message":
                    message = self._messages[action.message_key]
                    request = message.request
                    if message.peer is not None:
                        self.verify_peer(message.peer)
                elif action.kind == "acquire_decode":
                    # Each service rank initially proposes its own attempt. Only
                    # a fresh room is independent of the leader's eventual choice.
                    if self.control.has_retained_room(action.room):
                        raise ValueError("D admission requires a fresh bootstrap room")
                    peer = self.control.peer
                    if peer is None:
                        raise RuntimeError("D admission has no paired peer")
                    request = RequestIdentity(
                        action.room,
                        action.attempt,
                        peer.session,
                        self.control.local.session,
                    )
                claim = self.control.prepare(
                    request=request,
                    operation=lambda control: self._commit(
                        plan=(action,),
                        facts_by_room=facts_by_room,
                        records=records,
                        control=control,
                        preview=True,
                    ),
                )
                prepared.append(
                    _Preparation(key=action.preparation_key, record_claim=claim)
                )
            except Exception as exc:
                prepared.append(
                    _Preparation(key=action.preparation_key, error=str(exc))
                )
        return tuple(prepared)

    @staticmethod
    def _approve_plan(
        plan: Sequence[_Action], observations: Sequence[_Observation]
    ) -> list[_Action]:
        """Combine prepared candidates using only shared, immutable observations.

        One control transition per room and P/D slot keeps independent previews
        valid when composed. Conflicting work waits for the next tick. Admission
        spends the minimum rank capacity and runs last so it cannot evict a
        historical record still needed by another selected operation.
        """
        checks = [{p.key: p for p in o.prepared} for o in observations]
        states = {
            (r.room, r.attempt): r
            for o in observations
            for r in (*o.requests, *o.cleanup)
        }
        messages = {m.key: m for m in observations[0].messages}
        budget = min(o.record_capacity for o in observations)
        used: set[tuple[str, int]] = set()
        deferred: set[tuple[int, str]] = set()
        approved: list[_Action] = []
        admissions: list[_Action] = []
        for action in plan:
            if action.kind in ("reject", "native_release", "drain"):
                approved.append(action)
                continue
            resources = {("room", action.room)}
            state = states.get((action.room, action.attempt))
            p_slot, d_slot = (state.p_slot, state.d_slot) if state else (None, None)
            if action.kind == "message":
                message = messages[action.message_key]
                p_slot, d_slot = message.p_slot or p_slot, message.d_slot or d_slot
            if p_slot is not None:
                resources.add(("p_slot", p_slot[0]))
            if d_slot is not None:
                resources.add(("d_slot", d_slot[0]))
            if action.kind in ("acquire_prefill", "acquire_decode"):
                side = "p_slot" if action.kind == "acquire_prefill" else "d_slot"
                resources.add((side, action.slot))
            if used.intersection(resources):
                deferred.add((action.room, action.attempt))
                continue
            local = [check.get(action.preparation_key) for check in checks]
            if any(p is None for p in local):
                raise RuntimeError("mempool plan has an unprepared transition")
            errors = [p.error for p in local if p is not None and p.error]
            if errors:
                raise RuntimeError(errors[0])
            claims = {p.record_claim for p in local if p is not None}
            if len(claims) != 1:
                raise RuntimeError("TP mempool admission preparation diverged")
            claim = claims.pop()
            if claim > budget:
                deferred.add((action.room, action.attempt))
                continue
            budget -= claim
            used.update(resources)
            (admissions if claim else approved).append(action)
        return [
            action
            for action in (*approved, *admissions)
            if (action.room, action.attempt) not in deferred
        ]

    @staticmethod
    def _message_key(message: MempoolMessage) -> tuple[Any, ...]:
        """Match logical events across TP without comparing pair-local proofs."""
        request = message.request
        return (
            message.kind.value,
            request.room if request else -1,
            request.attempt if request else "",
        )

    def _plan(self, observations: Sequence[_Observation]) -> list[_Action]:
        """Prioritize cancellation and retirement; only approve common facts."""
        first = observations[0]
        if len(observations) != self.control.local.tp_size:
            raise RuntimeError("mempool tick did not gather the full TP group")
        if any(
            o.available_slots != first.available_slots or o.requests != first.requests
            for o in observations
        ):
            raise RuntimeError("TP mempool ownership or phase diverged")
        facts = [{f.room: f for f in o.facts} for o in observations]
        records = {(r.room, r.attempt): r for r in first.requests}
        # Native queues may retire their last Req on different ticks. Compare
        # active protocol state uniformly, but merge optional cleanup records.
        # A retained terminal record must never look like a new D admission.
        for observation in observations:
            for record in observation.cleanup:
                request_key = (record.room, record.attempt)
                previous = records.get(request_key)
                if previous is not None and previous != record:
                    raise RuntimeError("TP mempool cleanup state diverged")
                records[request_key] = record
        messages = [{m.key: m for m in o.messages} for o in observations]
        common = set.intersection(*(set(m) for m in messages))
        priority = {
            "CANCEL": 0,
            "DONE": 1,
            "RELEASE_ACK": 2,
            "POOL_HELLO": 3,
            "POOL_READY": 3,
        }
        plan: list[_Action] = []
        busy: set[tuple[int, str]] = set()
        for key in sorted(common, key=lambda k: (priority.get(k[0], 4), k)):
            _, room, attempt = key
            if (room, attempt) in busy:
                continue
            if key[0] == "DONE" and self.control.local.role == "prefill":
                owned = any(
                    r.room == room and r.attempt == attempt and r.owns_slot
                    for r in first.requests
                )
                # Detach any local attachment before returning its physical slot.
                if owned and any(room in f and not f[room].native_freed for f in facts):
                    continue
            if len({m[key] for m in messages}) != 1:
                raise RuntimeError("TP peers supplied inconsistent request metadata")
            plan.append(_Action("message", room, attempt, message_key=key))
            if key[0] == "CANCEL":
                plan.append(_Action("drain", room, attempt))
            busy.add((room, attempt))
        free = sorted(first.available_slots)
        for request_key in sorted(records):
            record = records[request_key]
            room, attempt = record.room, record.attempt
            if (room, attempt) in busy:
                continue
            local = [
                f[room] if room in f and f[room].attempt == attempt else None
                for f in facts
            ]
            present = all(f is not None for f in local)

            def all_fact(name: str) -> bool:
                """Require the same completion fact from the entire TP side."""
                return present and all(bool(getattr(f, name)) for f in local)

            terminal = record.phase in (
                "CANCELLED",
                "CLOSED",
                "RELEASED",
                "WAITING_RELEASE_ACK",
            )
            expired = (
                local[0] is not None
                and first.now >= local[0].deadline
                and record.phase
                in ("ACQUIRED", "WAITING_ACQUIRE", "BOUND", "WAITING_READY")
            )
            cancelling = any(f is not None and f.cancel for f in local) or expired
            if (
                cancelling
                and not terminal
                and record.phase not in ("CANCELLING", "DRAINING")
            ):
                plan.append(_Action("cancel", room, attempt))
                plan.append(_Action("drain", room, attempt))
                continue
            if (
                terminal
                and present
                and any(f.cancel and not f.release for f in local if f is not None)
            ):
                plan.append(_Action("reject", room, attempt))
                plan.append(_Action("drain", room, attempt))
                continue
            if self.control.local.role == "prefill":
                if (
                    present
                    and any(
                        f.cancel and f.release and not f.native_freed
                        for f in local
                        if f is not None
                    )
                    and not all_fact("host_drained")
                ):
                    plan.append(_Action("drain", room, attempt))
                if record.phase == "WAITING_ACQUIRE" and free:
                    plan.append(_Action("acquire_prefill", room, attempt, free.pop(0)))
                elif record.phase == "BOUND" and present:
                    if any(
                        f.prompt_tokens != record.prompt_limit
                        for f in local
                        if f is not None
                    ):
                        raise ValueError("P prompt length differs from D acquire")
                    plan.append(_Action("start_prefill", room, attempt))
                elif record.phase == "PREFILLING" and all_fact("prompt_ready"):
                    plan.append(_Action("ready", room, attempt))
                elif (
                    record.phase == "CANCELLING"
                    and record.writes_pending
                    and all_fact("writes_done")
                ):
                    plan.append(_Action("writes_done", room, attempt))
                if all_fact("native_release"):
                    plan.append(_Action("native_release", room, attempt))
            else:
                if record.phase == "WAITING_READY":
                    if not record.transfer_ready and all_fact("transfer_ready"):
                        plan.append(_Action("transfer", room, attempt))
                    elif record.transfer_ready and record.prompt_written is not None:
                        if record.prompt_written != record.prompt_limit:
                            raise ValueError(
                                "KV_READY does not cover the entire prompt"
                            )
                        plan.append(_Action("start_decode", room, attempt))
                elif (
                    record.phase == "CANCELLED"
                    and present
                    and not all_fact("native_freed")
                ):
                    if all_fact("drained"):
                        plan.append(_Action("native_release", room, attempt))
                    elif not all_fact("host_drained"):
                        plan.append(_Action("drain", room, attempt))
                elif record.phase in ("DECODING", "CANCELLING", "DRAINING"):
                    finishing = record.phase != "DECODING" or any(
                        f and f.release for f in local
                    )
                    if finishing:
                        if all_fact("drained"):
                            plan.append(_Action("release", room, attempt))
                        elif not all_fact("host_drained"):
                            plan.append(_Action("drain", room, attempt))
        if self.control.local.role == "prefill":
            occupied = {r.room for r in records.values()}
            for fact in first.facts:
                local = [f.get(fact.room) for f in facts]
                if fact.room in occupied or not all(f is not None for f in local):
                    continue
                if (
                    any(f.cancel for f in local if f is not None)
                    or first.now >= fact.deadline
                ):
                    if not all(f.release for f in local if f is not None):
                        plan.append(_Action("reject", fact.room))
                        plan.append(_Action("drain", fact.room))
                    elif not all(f.host_drained for f in local if f is not None):
                        plan.append(_Action("drain", fact.room))
                    elif all(f.native_release for f in local if f is not None):
                        plan.append(_Action("native_release", fact.room))
        if self.control.local.role == "decode" and all(
            o.peer_ready for o in observations
        ):
            occupied = {r.room for r in records.values()}
            for fact in sorted(first.facts, key=lambda f: f.room):
                if (
                    fact.native_freed
                    or fact.room in occupied
                    or not all(fact.room in f for f in facts)
                ):
                    continue
                if (
                    any(f[fact.room].cancel for f in facts)
                    or first.now >= fact.deadline
                ):
                    plan.append(_Action("reject", fact.room))
                    plan.append(_Action("drain", fact.room))
                    plan.append(_Action("native_release", fact.room))
                elif free:
                    if (
                        len(
                            {
                                (f[fact.room].prompt_tokens, f[fact.room].decode_tokens)
                                for f in facts
                            }
                        )
                        != 1
                    ):
                        raise ValueError("TP request capacities differ")
                    plan.append(
                        _Action("acquire_decode", fact.room, fact.attempt, free.pop(0))
                    )
        order = {
            "cancel": 0,
            "reject": 0,
            "message": 0,
            "drain": 1,
            "release": 2,
            "native_release": 2,
            "writes_done": 2,
            "acquire_prefill": 9,
            "acquire_decode": 9,
        }
        return sorted(plan, key=lambda action: order.get(action.kind, 3))

    def _commit(
        self,
        plan: Sequence[_Action],
        facts_by_room: Mapping[int, RequestObservation],
        records: dict[tuple[int, str], MempoolRequestSnapshot],
        control: MempoolPDControl,
        *,
        preview: bool = False,
    ) -> list[MempoolMessage]:
        """Reuse control transitions for preparation; run effects only on commit."""
        outbox = []
        drained = False
        for action in plan:
            kind, room = action.kind, action.room
            record = records.get((room, action.attempt))
            request = record.identity if record else None
            reply = None
            if kind == "message":
                message = self._messages[action.message_key]
                request = message.request
                reply = control.apply(message)
                if not preview:
                    self._last_peer_seen = self.clock()
                    if message.kind == MessageType.CANCEL and room in facts_by_room:
                        self.effect("cancel", room)
                    if message.kind == MessageType.POOL_HELLO:
                        self.endpoint = message.reply_to
                    del self._messages[action.message_key]
            elif kind == "acquire_decode":
                fact = facts_by_room[room]
                reply = control.acquire_decode(
                    room,
                    action.attempt,
                    action.slot,
                    fact.prompt_tokens,
                    fact.decode_tokens,
                    self.reply_to,
                )
                request = reply.request
            elif kind in ("reject", "native_release", "drain"):
                if not preview and (kind != "drain" or not drained):
                    self.effect(kind, room)
                    drained |= kind == "drain"
            else:
                if request is None:
                    raise RuntimeError("planned mempool attempt disappeared")
                if kind == "cancel":
                    reply = control.cancel_local(
                        request, "cancelled or bootstrap timeout"
                    )
                    if not preview:
                        self.effect("cancel", room)
                elif kind == "acquire_prefill":
                    reply = control.acquire_prefill(request, action.slot)
                elif kind == "start_prefill":
                    control.start_prefill(request)
                elif kind == "ready":
                    reply = control.finish_prefill_writes(request)
                    if reply is None:
                        reply = control.publish_kv_ready(
                            request, facts_by_room[room].prompt_tokens
                        )
                elif kind == "writes_done":
                    reply = control.finish_prefill_writes(request)
                elif kind == "transfer":
                    control.transfer_succeeded(request)
                elif kind == "start_decode":
                    control.start_decode(request)
                elif kind == "release":
                    if not preview:
                        self.effect("native_release", room)
                    if record is None or record.phase != "DRAINING":
                        control.begin_drain(request)
                    reply = control.finish_drain(request)
                else:
                    raise RuntimeError(f"unknown mempool tick action {kind}")
            if reply is not None:
                outbox.append(reply)
            if not preview and kind != "drain":
                current = (
                    control.get_request(request) if request is not None else record
                )
                p_slot = current.p_slot if current else None
                d_slot = current.d_slot if current else None
                logger.info(
                    "mempool role=%s rank=%s event=%s room=%s attempt=%s "
                    "p_slot=%s p_generation=%s d_slot=%s d_generation=%s phase=%s "
                    "send=%s free=%s",
                    control.local.role,
                    control.local.tp_rank,
                    action.message_key[0] if kind == "message" else kind,
                    room,
                    action.attempt,
                    p_slot.slot if p_slot else -1,
                    p_slot.generation if p_slot else -1,
                    d_slot.slot if d_slot else -1,
                    d_slot.generation if d_slot else -1,
                    current.phase if current else "NONE",
                    reply.kind.value if reply else "NONE",
                    len(control.available_slots()),
                )
        return outbox
