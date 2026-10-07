"""Persistent request ownership for Ascend sparse-KV mempool PD."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections import deque
from copy import copy
from dataclasses import dataclass, replace
from queue import Empty, SimpleQueue
from threading import Lock
from typing import Callable, Iterable

from .mempool_protocol import (
    MempoolMessage,
    MessageType,
    PoolPeer,
    RequestIdentity,
    SlotLease,
    decode_message,
    is_mempool_message,
    validate_peer,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MempoolRequestSnapshot:
    """Expose one attempt's protocol facts without handing out its mutable record.

    Slot coordinates can outlive local ownership, notably while D waits for ACK.
    owns_slot reports current local ownership, independently of retained leases.
    binding_confirmed/writes_pending are P facts; transfer_ready is a D fact.
    pending_done contains only unacknowledged DONE; released attempts expose ACK.
    """

    identity: RequestIdentity
    phase: str
    prompt_limit: int
    decode_limit: int
    reply_to: str
    d_slot: SlotLease
    p_slot: SlotLease | None
    owns_slot: bool
    prompt_written: int | None
    transfer_ready: bool
    writes_pending: bool
    binding_confirmed: bool
    pending_done: MempoolMessage | None
    done: MempoolMessage | None
    release_ack: MempoolMessage | None


@dataclass(frozen=True)
class MempoolControlSnapshot:
    """Capture scheduler-owned state as immutable, serializable observations.

    Read on the scheduler thread between transitions. Reading neither drains
    the inbox nor advances protocol state; faults remain visible to the TP tick.
    """

    local: PoolPeer
    peer: PoolPeer | None
    requests: tuple[MempoolRequestSnapshot, ...]
    available_slots: frozenset[int]
    protocol_fault: str | None


@dataclass
class _RequestRecord:
    """Retain one attempt independently of ordinary PD sender/receiver cleanup."""

    identity: RequestIdentity
    phase: str
    prompt_limit: int
    decode_limit: int
    reply_to: str
    d_slot: SlotLease
    p_slot: SlotLease | None = None
    prompt_written: int | None = None
    transfer_ready: bool = False
    writes_pending: bool = False
    pending_done: MempoolMessage | None = None
    done: MempoolMessage | None = None
    release_ack: MempoolMessage | None = None
    binding_confirmed: bool = False
    terminal_recorded: bool = False


class MempoolFrameRouter:
    """Demultiplex a PD PULL reader before or after mempool control attaches."""

    def __init__(self, max_pending: int = 128) -> None:
        """Bound frames received during the parent PD manager's startup."""
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self._max_pending = max_pending
        self._pending: deque[MempoolMessage] = deque()
        self._control: MempoolPDControl | None = None
        self._fault: str | None = None
        self._lock = Lock()

    def attach(self, control: MempoolPDControl) -> None:
        """Move early messages to the scheduler inbox in arrival order."""
        with self._lock:
            if self._control is not None:
                raise RuntimeError("Ascend mempool control is already attached")
            self._control = control
            while self._pending:
                control.enqueue(self._pending.popleft())
            if self._fault is not None:
                control.fail_protocol(self._fault)

    def route(self, frames: list[bytes]) -> bool:
        """Consume a tagged frame without ever terminating the shared PD reader."""
        if not is_mempool_message(frames):
            return False
        try:
            message = decode_message(frames)
        except Exception as exc:
            self._fail(f"invalid mempool control frame: {exc}")
            return True
        with self._lock:
            if self._fault is not None:
                return True
            if self._control is not None:
                self._control.enqueue(message)
            elif len(self._pending) < self._max_pending:
                self._pending.append(message)
            else:
                self._fail_locked("mempool control startup buffer is full")
        return True

    def wrap_receive(
        self, receive: Callable[[], list[bytes] | None]
    ) -> Callable[[], list[bytes] | None]:
        """Wrap the existing socket reader while passing ordinary PD frames through."""

        def receive_ascend() -> list[bytes] | None:
            """Queue mempool events and return only ordinary PD traffic."""
            frames = receive()
            if frames is None or not self.route(frames):
                return frames
            return None

        return receive_ascend

    def _fail(self, reason: str) -> None:
        """Record the first protocol fault without throwing on the network thread."""
        with self._lock:
            self._fail_locked(reason)

    def _fail_locked(self, reason: str) -> None:
        """Publish a sticky admission fault while holding the router lock."""
        if self._fault is None:
            self._fault = reason
            logger.error("Ascend mempool control fault: %s", reason)
            if self._control is not None:
                self._control.fail_protocol(reason)


class MempoolPDControl:
    """Own one pair's protocol state; ZMQ queues facts for the scheduler.

    The TP tick must approve every ownership-changing call, including apply,
    cancellation, finish_drain and finish_prefill_writes (which can consume a
    pending DONE). Completion/network callbacks must not call these transitions.
    """

    def __init__(
        self,
        local: PoolPeer,
        max_records: int = 4096,
    ) -> None:
        """Keep physical slots separate from SGLang request-pool indices."""
        if max_records <= 0:
            raise ValueError("mempool control limits must be positive")
        self.local = local
        self.peer: PoolPeer | None = None
        self._records: dict[RequestIdentity, _RequestRecord] = {}
        self._active_requests: set[RequestIdentity] = set()
        self._room_record_counts: dict[int, int] = {}
        self._max_records = max_records
        self._terminal_order: deque[RequestIdentity] = deque()
        self._seen_d_generation = [0] * local.layout.slots
        self._room_owner: dict[int, RequestIdentity] = {}
        self._slot_owner: dict[int, RequestIdentity] = {}
        self._generation = [0] * local.layout.slots
        self._retired_generation = [0] * local.layout.slots
        self._inbox: SimpleQueue[MempoolMessage] = SimpleQueue()
        self._protocol_fault: str | None = None
        self._lease_key = secrets.token_bytes(32)

    def fail_protocol(self, reason: str) -> None:
        """Prevent request admission after an invalid or lost tagged frame."""
        if self._protocol_fault is None:
            self._protocol_fault = reason

    def assert_healthy(self) -> None:
        """Surface receive-thread protocol faults on the scheduler thread."""
        if self._protocol_fault is not None:
            raise RuntimeError(self._protocol_fault)

    def enqueue(self, message: MempoolMessage) -> None:
        """Pass a decoded ZMQ event to the scheduler without changing ownership."""
        self._inbox.put(message)

    def enqueue_frames(self, frames: list[bytes]) -> bool:
        """Queue tagged ZMQ traffic and leave ordinary PD frames untouched."""
        if not is_mempool_message(frames):
            return False
        self.enqueue(decode_message(frames))
        return True

    def drain_inbox(self) -> list[MempoolMessage]:
        """Take all pending network events for ordered scheduler processing."""
        self.assert_healthy()
        messages = []
        while True:
            try:
                messages.append(self._inbox.get_nowait())
            except Empty:
                return messages

    def begin_handshake(self, reply_to: str) -> MempoolMessage:
        """Ask the paired P rank to validate this D rank before request admission."""
        self.assert_healthy()
        self._require_role("decode")
        return MempoolMessage(
            MessageType.POOL_HELLO, peer=self.local, reply_to=reply_to
        )

    def available_slots(self) -> frozenset[int]:
        """Expose candidates for the scheduler's rank-wide same-slot choice."""
        return frozenset(set(range(self.local.layout.slots)) - self._slot_owner.keys())

    def snapshot(self) -> MempoolControlSnapshot:
        """Copy all retained records for diagnostics, including terminal history."""
        return self._snapshot(self._records)

    def get_request(self, request: RequestIdentity) -> MempoolRequestSnapshot | None:
        """Read one exact attempt without scanning unrelated request history."""
        record = self._records.get(request)
        return self._request_snapshot(record) if record is not None else None

    def get_room_request(self, room: int) -> MempoolRequestSnapshot | None:
        """Resolve the current owner before a service request knows its identity."""
        request = self._room_owner.get(room)
        return self.get_request(request) if request is not None else None

    def has_retained_room(self, room: int) -> bool:
        """Keep the service's fresh-room admission rule independent of history size."""
        return room in self._room_record_counts

    def has_pending_requests(self) -> bool:
        """Include requests waiting for ACK even after their local slot is free."""
        return bool(self._active_requests)

    def active_snapshot(
        self, include: Iterable[RequestIdentity] = ()
    ) -> MempoolControlSnapshot:
        """Observe protocol work plus attempts still needed for local cleanup."""
        return self._snapshot(self._active_requests.union(include))

    def _snapshot(self, requests: Iterable[RequestIdentity]) -> MempoolControlSnapshot:
        return MempoolControlSnapshot(
            local=self.local,
            peer=self.peer,
            requests=tuple(
                self._request_snapshot(self._records[request])
                for request in requests
                if request in self._records
            ),
            available_slots=self.available_slots(),
            protocol_fault=self._protocol_fault,
        )

    def _request_snapshot(self, record: _RequestRecord) -> MempoolRequestSnapshot:
        lease = record.p_slot if self.local.role == "prefill" else record.d_slot
        return MempoolRequestSnapshot(
            identity=record.identity,
            phase=record.phase,
            prompt_limit=record.prompt_limit,
            decode_limit=record.decode_limit,
            reply_to=record.reply_to,
            d_slot=record.d_slot,
            p_slot=record.p_slot,
            owns_slot=lease is not None
            and self._slot_owner.get(lease.slot) == record.identity,
            prompt_written=record.prompt_written,
            transfer_ready=record.transfer_ready,
            writes_pending=record.writes_pending,
            binding_confirmed=record.binding_confirmed,
            pending_done=record.pending_done if record.release_ack is None else None,
            done=record.done,
            release_ack=record.release_ack,
        )

    def state(self, request: RequestIdentity) -> str:
        """Report an attempt's durable lifecycle state for scheduler decisions."""
        return self._record(request).phase

    def admission_capacity(self) -> int:
        """Count records that can be admitted after evicting terminal history."""
        return self._max_records - len(self._active_requests)

    def prepare(
        self,
        request: RequestIdentity | None,
        operation: Callable[[MempoolPDControl], object],
    ) -> int:
        """Validate one candidate on isolated state and return its record claim.

        Only this attempt may change. Reuse the real transitions, including
        pair-local proofs and retirement checks, without copying history. The
        tick combines candidates against admission_capacity and available_slots;
        this preview deliberately does not spend the shared admission budget.
        Ownership stays on the scheduler thread between preparation and commit.
        """
        preview = copy(self)
        record = self._records.get(request) if request is not None else None
        preview._records = {record.identity: replace(record)} if record else {}
        preview._active_requests = (
            {request} if request in self._active_requests else set()
        )
        preview._room_record_counts = (
            {request.room: self._room_record_counts.get(request.room, 0)}
            if request is not None
            else {}
        )
        preview._terminal_order = (
            deque((record.identity,))
            if record is not None and record.terminal_recorded
            else deque()
        )
        preview._seen_d_generation = self._seen_d_generation.copy()
        owner = self._room_owner.get(request.room) if request is not None else None
        preview._room_owner = {owner.room: owner} if owner is not None else {}
        preview._slot_owner = self._slot_owner.copy()
        preview._generation = self._generation.copy()
        preview._retired_generation = self._retired_generation.copy()
        preview._inbox = SimpleQueue()
        operation(preview)
        if any(identity != request for identity in preview._records):
            raise RuntimeError("mempool preparation changed another request")
        return int(record is None and request in preview._records)

    def reply_endpoint(self, request: RequestIdentity) -> str:
        """Return the D rank's existing ZMQ endpoint for P-side replies."""
        self._require_role("prefill")
        return self._record(request).reply_to

    def acquire_decode(
        self,
        room: int,
        attempt: str,
        slot: int,
        prompt_tokens: int,
        decode_tokens: int,
        reply_to: str,
    ) -> MempoolMessage:
        """Reserve the scheduler-chosen D slot and request independent P acquire."""
        self.assert_healthy()
        self._require_role("decode")
        peer = self._require_peer()
        if type(prompt_tokens) is not int or not (
            0 <= prompt_tokens <= self.local.layout.prompt_tokens
        ):
            raise ValueError("prompt length exceeds mempool capacity")
        if type(decode_tokens) is not int or not (
            0 <= decode_tokens <= self.local.layout.decode_tokens
        ):
            raise ValueError("decode limit exceeds mempool capacity")
        if not isinstance(reply_to, str) or not reply_to.strip():
            raise ValueError("reply_to must be a nonempty string")
        request = RequestIdentity(room, attempt, peer.session, self.local.session)
        if request in self._records:
            record = self._records[request]
            if record.phase != "ACQUIRED":
                raise ValueError("request attempt is already beyond acquire")
            if (
                record.d_slot.slot != slot
                or record.prompt_limit != prompt_tokens
                or record.decode_limit != decode_tokens
                or record.reply_to != reply_to
            ):
                raise ValueError("retry has conflicting acquire metadata")
            return self._acquire_message(record)
        self._require_room_free(request)
        self._make_record_room()
        lease = self._reserve(slot, request)
        record = _RequestRecord(
            request, "ACQUIRED", prompt_tokens, decode_tokens, reply_to, lease
        )
        self._remember_request(record)
        return self._acquire_message(record)

    def acquire_prefill(self, request: RequestIdentity, slot: int) -> MempoolMessage:
        """Reserve the scheduler-chosen P slot after every P rank can use it."""
        self.assert_healthy()
        self._require_role("prefill")
        record = self._record(request)
        if record.phase == "ACQUIRED" and record.p_slot is not None:
            if record.p_slot.slot != slot:
                raise ValueError("request already acquired a different P slot")
            return self._acquired_message(record)
        if record.phase != "WAITING_ACQUIRE":
            raise ValueError("P request is not waiting for acquire")
        lease = self._reserve(slot, request)
        record.p_slot = SlotLease(
            lease.slot,
            lease.generation,
            self._lease_proof(request, lease, record.d_slot),
        )
        record.phase = "ACQUIRED"
        return self._acquired_message(record)

    def start_prefill(self, request: RequestIdentity) -> None:
        """Permit P computation only after D confirmed the complete binding."""
        self.assert_healthy()
        self._require_role("prefill")
        record = self._record(request)
        if record.phase != "BOUND":
            raise ValueError("P request has not received BOUND_ACK")
        record.phase = "PREFILLING"
        record.writes_pending = True

    def finish_prefill_writes(self, request: RequestIdentity) -> MempoolMessage | None:
        """Confirm the caller drained BM writes, then answer any early DONE."""
        self._require_role("prefill")
        record = self._record(request)
        if record.phase not in ("PREFILLING", "CANCELLING"):
            raise ValueError("P request has no prefill writes to drain")
        record.writes_pending = False
        if record.pending_done is not None:
            return self._release_prefill(record)
        return None

    def publish_kv_ready(
        self, request: RequestIdentity, prompt_tokens: int
    ) -> MempoolMessage:
        """Publish readable prompt KV only after an explicit write drain."""
        self.assert_healthy()
        self._require_role("prefill")
        record = self._record(request)
        if record.phase != "PREFILLING":
            raise ValueError("P request is not completing prefill writes")
        if record.writes_pending:
            raise ValueError("P mempool writes have not drained")
        if not 0 <= prompt_tokens <= record.prompt_limit:
            raise ValueError("written prompt KV exceeds acquired capacity")
        record.prompt_written = prompt_tokens
        record.phase = "WAITING_DONE"
        return MempoolMessage(
            MessageType.KV_READY,
            request=request,
            p_slot=record.p_slot,
            d_slot=record.d_slot,
            prompt_tokens=prompt_tokens,
        )

    def transfer_succeeded(self, request: RequestIdentity) -> None:
        """Record success of the existing Index K/state/metadata transfer."""
        self._require_role("decode")
        record = self._record(request)
        if record.phase != "WAITING_READY":
            raise ValueError("D request is not waiting for handoff readiness")
        record.transfer_ready = True

    def can_decode(self, request: RequestIdentity) -> bool:
        """Require both remote prompt KV and the original PD transfer result."""
        self._require_role("decode")
        record = self._record(request)
        return (
            record.phase == "WAITING_READY"
            and record.prompt_written is not None
            and record.transfer_ready
        )

    def start_decode(self, request: RequestIdentity) -> None:
        """Enter decode only after the joint readiness condition holds."""
        self.assert_healthy()
        if not self.can_decode(request):
            raise ValueError("D request is missing KV_READY or transfer success")
        self._record(request).phase = "DECODING"

    def begin_drain(self, request: RequestIdentity) -> None:
        """Stop future work while retaining D storage for submitted NPU work."""
        self._require_role("decode")
        record = self._record(request)
        if record.phase not in ("DECODING", "CANCELLING"):
            raise ValueError("D request is not ready to drain")
        if record.p_slot is None:
            raise ValueError("unbound request must roll back instead of draining")
        record.phase = "DRAINING"

    def finish_drain(
        self, request: RequestIdentity, reason: str = "completed"
    ) -> MempoolMessage:
        """Release D storage only after the caller has drained all NPU accesses."""
        self._require_role("decode")
        record = self._record(request)
        if record.phase == "WAITING_RELEASE_ACK" and record.done is not None:
            return record.done
        if record.phase != "DRAINING":
            raise ValueError("D request has not drained")
        done = MempoolMessage(
            MessageType.DONE,
            request=request,
            p_slot=record.p_slot,
            d_slot=record.d_slot,
            reason=reason,
        )
        self._release_slot(record)
        record.done = done
        record.phase = "WAITING_RELEASE_ACK"
        return done

    def cancel_local(self, request: RequestIdentity, reason: str) -> MempoolMessage:
        """Stop new work; roll back only a binding that has never been used."""
        record = self._record(request)
        message = MempoolMessage(
            MessageType.CANCEL,
            request=request,
            p_slot=record.p_slot,
            d_slot=record.d_slot,
            reason=reason,
        )
        self._cancel_record(record)
        return message

    def apply(self, message: MempoolMessage) -> MempoolMessage | None:
        """Apply a network event on the scheduler thread and return any reply."""
        self.assert_healthy()
        if message.kind == MessageType.POOL_HELLO:
            self._require_role("prefill")
            peer = message.peer
            self._accept_peer(peer)
            if peer is None:
                raise ValueError("POOL_HELLO has no peer")
            return MempoolMessage(
                MessageType.POOL_READY,
                peer=self.local,
                receiver_session=peer.session,
            )
        if message.kind == MessageType.POOL_READY:
            self._require_role("decode")
            if message.receiver_session != self.local.session:
                raise ValueError("POOL_READY acknowledges a different D session")
            self._accept_peer(message.peer)
            return None

        if message.kind == MessageType.HEARTBEAT:
            if (
                message.peer != self._require_peer()
                or message.receiver_session != self.local.session
            ):
                raise ValueError("mempool heartbeat uses a different pool session")
            return None
        self._validate_request(message.request)
        if message.kind == MessageType.ACQUIRE:
            return self._accept_acquire(message)
        if message.kind == MessageType.ACQUIRED:
            return self._accept_acquired(message)
        if message.kind == MessageType.BOUND_ACK:
            self._accept_bound_ack(message)
        elif message.kind == MessageType.KV_READY:
            self._accept_kv_ready(message)
        elif message.kind == MessageType.CANCEL:
            return self._accept_cancel(message)
        elif message.kind == MessageType.DONE:
            return self._accept_done(message)
        elif message.kind == MessageType.RELEASE_ACK:
            self._accept_release_ack(message)
        else:
            raise ValueError(f"unexpected mempool message {message.kind.value}")
        return None

    def _accept_peer(self, peer: PoolPeer | None) -> None:
        """Bind one compatible startup session for this control instance."""
        if peer is None:
            raise ValueError("mempool handshake has no peer")
        validate_peer(self.local, peer)
        if self.peer is not None and self.peer != peer:
            raise ValueError("mempool peer session changed during this pool lifetime")
        self.peer = peer

    def _validate_request(self, request: RequestIdentity | None) -> None:
        """Reject messages from an earlier pool startup before slot lookup."""
        peer = self._require_peer()
        if request is None:
            raise ValueError("mempool request identity is missing")
        p_session = self.local.session if self.local.role == "prefill" else peer.session
        d_session = self.local.session if self.local.role == "decode" else peer.session
        if request.p_session != p_session or request.d_session != d_session:
            raise ValueError("mempool request uses a stale pool session")

    def _accept_acquire(self, message: MempoolMessage) -> MempoolMessage | None:
        """Record a P acquisition request without assigning a slot on ZMQ recv."""
        self._require_role("prefill")
        request = self._required_request(message)
        d_slot = message.d_slot
        prompt_tokens = message.prompt_tokens
        decode_tokens = message.decode_tokens
        reply_to = message.reply_to
        if (
            d_slot is None
            or prompt_tokens is None
            or decode_tokens is None
            or reply_to is None
        ):
            raise ValueError("ACQUIRE is missing required metadata")
        if prompt_tokens > self.local.layout.prompt_tokens:
            raise ValueError("ACQUIRE prompt length exceeds P mempool capacity")
        if decode_tokens > self.local.layout.decode_tokens:
            raise ValueError("ACQUIRE decode limit exceeds D mempool capacity")
        if d_slot.slot >= self.local.layout.slots:
            raise ValueError("ACQUIRE D slot is outside the paired pool")
        record = self._records.get(request)
        if record is not None:
            if (
                record.d_slot != d_slot
                or record.prompt_limit != prompt_tokens
                or record.decode_limit != decode_tokens
                or record.reply_to != reply_to
            ):
                raise ValueError("duplicate ACQUIRE has conflicting metadata")
            if record.phase not in (
                "WAITING_ACQUIRE",
                "CANCELLED",
                "CANCELLING",
                "RELEASED",
            ):
                return self._acquired_message(record)
            return None
        if d_slot.generation <= self._seen_d_generation[d_slot.slot]:
            return None
        self._require_room_free(request)
        self._make_record_room()
        record = _RequestRecord(
            request,
            "WAITING_ACQUIRE",
            prompt_tokens,
            decode_tokens,
            reply_to,
            d_slot,
        )
        self._remember_request(record)
        self._seen_d_generation[d_slot.slot] = d_slot.generation
        return None

    def _accept_acquired(self, message: MempoolMessage) -> MempoolMessage | None:
        """Save P's independent slot and acknowledge the complete binding."""
        self._require_role("decode")
        request = self._required_request(message)
        record = self._records.get(request)
        if record is None:
            if self._is_retired_local_lease(request, message.d_slot):
                return None
            raise ValueError("ACQUIRED has no matching D request")
        if message.p_slot is None:
            raise ValueError("ACQUIRED has no P slot")
        if message.p_slot.slot >= self.local.layout.slots:
            raise ValueError("ACQUIRED P slot is outside the paired pool")
        if record.d_slot != message.d_slot:
            raise ValueError("ACQUIRED does not match the D slot generation")
        if record.phase == "CANCELLED":
            return MempoolMessage(
                MessageType.CANCEL,
                request=record.identity,
                d_slot=record.d_slot,
                reason="D cancelled before accepting P binding",
            )
        if record.p_slot is None:
            if record.phase != "ACQUIRED":
                raise ValueError("D request cannot accept P binding now")
            record.p_slot = message.p_slot
            record.phase = "WAITING_READY"
        elif record.p_slot != message.p_slot:
            raise ValueError("ACQUIRED conflicts with existing P binding")
        return self._binding_message(MessageType.BOUND_ACK, record)

    def _accept_bound_ack(self, message: MempoolMessage) -> None:
        """Permit prefill only for the P slot and D generation already acquired."""
        self._require_role("prefill")
        record = self._records.get(self._required_request(message))
        if record is None:
            self._check_prior_release(message)
            return
        self._check_binding(record, message)
        if record.phase == "CANCELLED":
            self._check_prior_release(message)
            return
        if record.phase == "ACQUIRED":
            record.phase = "BOUND"
            record.binding_confirmed = True
        elif record.phase not in (
            "BOUND",
            "PREFILLING",
            "WAITING_DONE",
            "CANCELLING",
            "RELEASED",
        ):
            raise ValueError("BOUND_ACK arrived before P acquire")
        else:
            record.binding_confirmed = True

    def _accept_kv_ready(self, message: MempoolMessage) -> None:
        """Record P's completed prompt KV independently of transfer success."""
        self._require_role("decode")
        request = self._required_request(message)
        record = self._records.get(request)
        if record is None:
            if self._is_retired_local_lease(request, message.d_slot):
                return
            raise ValueError("KV_READY has no matching D request")
        self._check_binding(record, message)
        if record.phase in ("CANCELLING", "DRAINING", "WAITING_RELEASE_ACK", "CLOSED"):
            # A matching ready message may race with cancellation or completion.
            return
        if record.phase not in ("WAITING_READY", "DECODING"):
            raise ValueError("KV_READY arrived outside the active binding")
        if message.prompt_tokens is None:
            raise ValueError("KV_READY has no prompt length")
        if message.prompt_tokens > record.prompt_limit:
            raise ValueError("KV_READY exceeds acquired prompt capacity")
        if (
            record.prompt_written is not None
            and record.prompt_written != message.prompt_tokens
        ):
            raise ValueError("duplicate KV_READY has a different prompt length")
        record.prompt_written = message.prompt_tokens

    def _accept_cancel(self, message: MempoolMessage) -> MempoolMessage | None:
        """Keep bound storage until drain while dropping unused acquisitions."""
        request = self._required_request(message)
        record = self._records.get(request)
        if record is None:
            if self.local.role == "prefill" and message.d_slot is not None:
                if message.p_slot is not None:
                    self._check_prior_release(message)
                if message.d_slot.slot >= self.local.layout.slots:
                    raise ValueError("CANCEL D slot is outside the paired pool")
                self._seen_d_generation[message.d_slot.slot] = max(
                    self._seen_d_generation[message.d_slot.slot],
                    message.d_slot.generation,
                )
            return None
        if message.d_slot is not None and record.d_slot != message.d_slot:
            raise ValueError("CANCEL does not match the D slot generation")
        if (
            message.p_slot is not None
            and record.p_slot != message.p_slot
            and not (self.local.role == "decode" and record.p_slot is None)
        ):
            raise ValueError("CANCEL does not match the P slot generation")
        if self.local.role == "prefill":
            if message.p_slot is not None:
                record.binding_confirmed = True
            elif record.binding_confirmed:
                raise ValueError("unbound CANCEL conflicts with confirmed binding")
            self._cancel_record(record, unbound_confirmed=message.p_slot is None)
            return None
        was_unbound = record.p_slot is None
        self._cancel_record(record)
        if was_unbound and message.p_slot is not None:
            return MempoolMessage(
                MessageType.CANCEL,
                request=record.identity,
                d_slot=record.d_slot,
                reason="D did not accept P binding",
            )
        return None

    def _accept_done(self, message: MempoolMessage) -> MempoolMessage | None:
        """Release P only after matching D drain and local BM writes complete."""
        self._require_role("prefill")
        request = self._required_request(message)
        record = self._records.get(request)
        if record is None:
            return self._ack_prior_release(message)
        self._check_binding(record, message)
        if record.release_ack is not None:
            return record.release_ack
        if record.phase == "CANCELLED":
            return self._ack_prior_release(message)
        if record.phase not in ("BOUND", "PREFILLING", "WAITING_DONE", "CANCELLING"):
            raise ValueError("DONE arrived outside a bound P request")
        record.pending_done = message
        if record.writes_pending:
            record.phase = "CANCELLING"
            return None
        return self._release_prefill(record)

    def _ack_prior_release(self, message: MempoolMessage) -> MempoolMessage:
        """Confirm an allocation retired by DONE or safe rollback, without freeing again."""
        self._check_prior_release(message)
        return MempoolMessage(
            MessageType.RELEASE_ACK,
            request=message.request,
            p_slot=message.p_slot,
            d_slot=message.d_slot,
        )

    def _check_prior_release(self, message: MempoolMessage) -> None:
        """Combine a signed allocation with its slot retirement boundary and owner."""
        request = self._required_request(message)
        p_slot, d_slot = message.p_slot, message.d_slot
        slots = self.local.layout.slots
        if (
            p_slot is None
            or d_slot is None
            or p_slot.slot >= slots
            or d_slot.slot >= slots
            or p_slot.generation > self._retired_generation[p_slot.slot]
            or d_slot.generation > self._seen_d_generation[d_slot.slot]
            or self._slot_owner.get(p_slot.slot) == request
        ):
            raise ValueError("DONE has no matching prior P release")
        expected = self._lease_proof(request, p_slot, d_slot)
        if p_slot.proof is None or not hmac.compare_digest(p_slot.proof, expected):
            raise ValueError("mempool binding proof does not match prior release")

    def _accept_release_ack(self, message: MempoolMessage) -> None:
        """Close D control state without touching a reused physical slot."""
        self._require_role("decode")
        request = self._required_request(message)
        record = self._records.get(request)
        if record is None:
            if self._is_retired_local_lease(request, message.d_slot):
                return
            raise ValueError("RELEASE_ACK has no matching D request")
        self._check_binding(record, message)
        if record.phase == "WAITING_RELEASE_ACK":
            record.phase = "CLOSED"
            self._remember_terminal(record)
        elif record.phase != "CLOSED":
            raise ValueError("RELEASE_ACK arrived before D drain")

    def _release_prefill(self, record: _RequestRecord) -> MempoolMessage:
        """Return P's slot and retain the exact ACK for duplicate DONE."""
        if record.p_slot is None or record.p_slot.proof is None:
            raise RuntimeError("bound P request has no physical slot")
        self._release_slot(record)
        record.phase = "RELEASED"
        record.release_ack = self._binding_message(MessageType.RELEASE_ACK, record)
        self._remember_terminal(record)
        return record.release_ack

    def _cancel_record(
        self, record: _RequestRecord, unbound_confirmed: bool = False
    ) -> None:
        """Roll back unbound slots; preserve bound ones until DONE/drain."""
        if record.phase in (
            "DRAINING",
            "CANCELLED",
            "CLOSED",
            "RELEASED",
            "WAITING_RELEASE_ACK",
        ):
            return
        if record.p_slot is None or (
            self.local.role == "prefill"
            and unbound_confirmed
            and not record.binding_confirmed
        ):
            self._release_slot(record)
            record.phase = "CANCELLED"
            self._remember_terminal(record)
        else:
            record.phase = "CANCELLING"

    def _make_record_room(self) -> None:
        """Evict old terminal records before admitting another attempt."""
        while len(self._records) >= self._max_records and self._terminal_order:
            self._evict_terminal()
        if len(self._records) >= self._max_records:
            raise RuntimeError("mempool control record capacity reached")

    def _remember_terminal(self, record: _RequestRecord) -> None:
        """Retain recent ACKs while bounding terminal replay memory."""
        if record.terminal_recorded:
            return
        record.terminal_recorded = True
        self._active_requests.remove(record.identity)
        self._terminal_order.append(record.identity)
        while len(self._records) > self._max_records and self._terminal_order:
            self._evict_terminal()

    def _remember_request(self, record: _RequestRecord) -> None:
        """Index protocol work separately from bounded replay history."""
        request = record.identity
        self._records[request] = record
        self._active_requests.add(request)
        self._room_owner[request.room] = request
        self._room_record_counts[request.room] = (
            self._room_record_counts.get(request.room, 0) + 1
        )

    def _evict_terminal(self) -> None:
        request = self._terminal_order.popleft()
        del self._records[request]
        self._room_record_counts[request.room] -= 1
        if not self._room_record_counts[request.room]:
            del self._room_record_counts[request.room]

    def _check_binding(self, record: _RequestRecord, message: MempoolMessage) -> None:
        """Reject stale leases even when a bootstrap room has been reused."""
        if record.p_slot != message.p_slot or record.d_slot != message.d_slot:
            raise ValueError("mempool message has a stale slot generation")

    def _reserve(self, slot: int, request: RequestIdentity) -> SlotLease:
        """Reserve a rank-wide chosen slot and advance its local generation."""
        if type(slot) is not int or not 0 <= slot < self.local.layout.slots:
            raise ValueError("mempool slot is out of range")
        if slot in self._slot_owner:
            raise ValueError("mempool slot is already acquired")
        self._generation[slot] += 1
        self._slot_owner[slot] = request
        lease = SlotLease(slot, self._generation[slot])
        if self.local.role == "decode":
            lease = SlotLease(slot, lease.generation, self._lease_proof(request, lease))
        return lease

    def _lease_proof(
        self,
        request: RequestIdentity,
        lease: SlotLease,
        d_slot: SlotLease | None = None,
    ) -> str:
        """Sign a local slot generation and its request/binding identity."""
        payload = [
            self.local.role,
            request.room,
            request.attempt,
            request.p_session,
            request.d_session,
            lease.slot,
            lease.generation,
        ]
        if d_slot is not None:
            payload.extend((d_slot.slot, d_slot.generation, d_slot.proof))
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._lease_key, encoded, hashlib.sha256).hexdigest()

    def _is_retired_local_lease(
        self, request: RequestIdentity, lease: SlotLease | None
    ) -> bool:
        """Verify an old D lease without acting on a current slot owner."""
        if (
            self.local.role != "decode"
            or lease is None
            or lease.proof is None
            or lease.slot >= self.local.layout.slots
            or lease.generation > self._retired_generation[lease.slot]
            or self._slot_owner.get(lease.slot) == request
        ):
            return False
        return hmac.compare_digest(lease.proof, self._lease_proof(request, lease))

    def _release_slot(self, record: _RequestRecord) -> None:
        """Return only the physical slot still owned by this exact attempt."""
        lease = record.p_slot if self.local.role == "prefill" else record.d_slot
        if lease is not None:
            if self._slot_owner.get(lease.slot) != record.identity:
                raise RuntimeError("mempool slot ownership changed before release")
            del self._slot_owner[lease.slot]
            # Each slot is reused serially: this boundary covers every older use.
            # Advance only on actual release, including an unused-slot rollback.
            self._retired_generation[lease.slot] = lease.generation
        if self._room_owner.get(record.identity.room) == record.identity:
            del self._room_owner[record.identity.room]

    def _require_room_free(self, request: RequestIdentity) -> None:
        """Prevent two live attempts from claiming one bootstrap room."""
        existing = self._room_owner.get(request.room)
        if existing is not None and existing != request:
            raise ValueError("bootstrap room already has an active mempool request")

    def _record(self, request: RequestIdentity) -> _RequestRecord:
        """Fetch an attempt without conflating it with a later room occupant."""
        record = self._records.get(request)
        if record is None:
            raise ValueError("unknown mempool request attempt")
        return record

    @staticmethod
    def _required_request(message: MempoolMessage) -> RequestIdentity:
        """Extract the request that all non-handshake messages must carry."""
        if message.request is None:
            raise ValueError("mempool control message has no request")
        return message.request

    def _require_peer(self) -> PoolPeer:
        """Require the ZMQ peer handshake before request control begins."""
        if self.peer is None:
            raise ValueError("mempool peer control is not ready")
        return self.peer

    def _require_role(self, role: str) -> None:
        """Reject a transition attempted by the wrong PD side."""
        if self.local.role != role:
            raise ValueError(f"{role} operation called on {self.local.role} control")

    @staticmethod
    def _binding_message(kind: MessageType, record: _RequestRecord) -> MempoolMessage:
        """Build a reply containing both exact slot generations."""
        return MempoolMessage(
            kind,
            request=record.identity,
            p_slot=record.p_slot,
            d_slot=record.d_slot,
        )

    @staticmethod
    def _acquire_message(record: _RequestRecord) -> MempoolMessage:
        """Rebuild the original ACQUIRE for an idempotent local retry."""
        return MempoolMessage(
            MessageType.ACQUIRE,
            request=record.identity,
            d_slot=record.d_slot,
            reply_to=record.reply_to,
            prompt_tokens=record.prompt_limit,
            decode_tokens=record.decode_limit,
        )

    @staticmethod
    def _acquired_message(record: _RequestRecord) -> MempoolMessage:
        """Rebuild the original binding reply for a duplicate ACQUIRE."""
        return MempoolPDControl._binding_message(MessageType.ACQUIRED, record)
