"""Advance one side's sixteen persistent controls in a fixed collective order."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .mempool_control import MempoolPDControl, MempoolRequestSnapshot
from .mempool_protocol import MempoolMessage, MessageType, PoolPeer, RequestIdentity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestObservation:
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


@dataclass(frozen=True)
class _RequestState:
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


@dataclass(frozen=True)
class _MessageState:
    """Agree on an event's metadata; validate its original message in preflight."""

    key: tuple[Any, ...]
    prompt_tokens: int | None
    decode_tokens: int | None
    p_slot: tuple[int, int] | None
    d_slot: tuple[int, int] | None


@dataclass(frozen=True)
class _Observation:
    """Serialize one worker's durable observations for the same-side TP group."""

    requests: tuple[_RequestState, ...]
    cleanup: tuple[_RequestState, ...]
    available_slots: frozenset[int]
    peer_ready: bool
    facts: tuple[RequestObservation, ...]
    messages: tuple[_MessageState, ...]
    now: float
    fault: str | None


@dataclass(frozen=True)
class _Action:
    """Identify an agreed operation without transporting another pair's proofs."""

    kind: str
    room: int = -1
    attempt: str = ""
    slot: int = -1
    message_key: tuple[Any, ...] = ()


class MempoolTPTick:
    """Own snapshot/plan/preflight/commit/status and hold outbox until success.

    gather is the existing TP CPU group's all-gather boundary. Network threads
    only enqueue; even an empty advance participates in three collectives.
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
        )
        observations = self.gather(observation)
        plan: list[_Action] = []
        error = None
        try:
            if any(o.fault for o in observations):
                raise RuntimeError("mempool worker reported a fault")
            plan = self._plan(observations)
            for action in plan:
                if action.kind == "message":
                    peer = self._messages[action.message_key].peer
                    if peer is not None:
                        self.verify_peer(peer)
            self.control.preflight(
                lambda control: self._commit(
                    plan, facts, records, control, preview=True
                )
            )
        except Exception as exc:
            error = str(exc)
        errors = self.gather(error)
        outbox: list[MempoolMessage] = []
        if not any(errors):
            try:
                outbox = self._commit(plan, facts, records, self.control)
            except Exception as exc:
                error = str(exc)
        else:
            error = next(e for e in errors if e)
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
                if fact.cancel or first.now >= fact.deadline:
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
        facts: Sequence[RequestObservation],
        records: dict[tuple[int, str], MempoolRequestSnapshot],
        control: MempoolPDControl,
        *,
        preview: bool = False,
    ) -> list[MempoolMessage]:
        """Run identical transitions during preflight and commit; effects only commit."""
        by_room = {f.room: f for f in facts}
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
                    if message.kind == MessageType.CANCEL and room in by_room:
                        self.effect("cancel", room)
                    if message.kind == MessageType.POOL_HELLO:
                        self.endpoint = message.reply_to
                    del self._messages[action.message_key]
            elif kind == "acquire_decode":
                fact = by_room[room]
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
                            request, by_room[room].prompt_tokens
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
