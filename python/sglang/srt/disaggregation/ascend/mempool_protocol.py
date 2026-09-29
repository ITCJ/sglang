"""Typed control messages carried by the existing Ascend PD ZMQ sockets."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, TypeVar

WIRE_TAG = b"ASCEND_MEMPOOL_V1"
PROTOCOL_VERSION = 1
MAX_WIRE_BYTES = 64 * 1024


class MessageType(str, Enum):
    """Name the mempool messages without changing ordinary PD message framing."""

    POOL_HELLO = "POOL_HELLO"
    POOL_READY = "POOL_READY"
    ACQUIRE = "ACQUIRE"
    ACQUIRED = "ACQUIRED"
    BOUND_ACK = "BOUND_ACK"
    KV_READY = "KV_READY"
    CANCEL = "CANCEL"
    DONE = "DONE"
    RELEASE_ACK = "RELEASE_ACK"


def _positive_int(name: str, value: object) -> None:
    """Reject bool and nonpositive sizes in untrusted peer metadata."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _nonnegative_int(name: str, value: object) -> None:
    """Reject bool and negative indices in untrusted peer metadata."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _nonempty_string(name: str, value: object) -> None:
    """Require a usable opaque identifier or reply endpoint."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class PoolDescriptor:
    """Describe the identical logical layout expected by both paired workers."""

    layers: int
    slots: int
    prompt_tokens: int
    decode_tokens: int
    heads: int
    dim: int
    dtype: str
    prompt_bytes: int
    decode_bytes: int
    stride_bytes: int

    def __post_init__(self) -> None:
        """Check schema and physical bounds before peer comparison."""
        for name in (
            "layers",
            "slots",
            "prompt_tokens",
            "decode_tokens",
            "heads",
            "dim",
            "prompt_bytes",
            "decode_bytes",
            "stride_bytes",
        ):
            _positive_int(name, getattr(self, name))
        if self.slots != 16:
            raise ValueError("mempool PD requires 16 slots")
        if self.dtype != "bfloat16":
            raise ValueError("mempool PD requires bfloat16 KV")
        row_bytes = self.heads * self.dim * 2
        if (
            self.prompt_bytes
            < self.layers * self.slots * self.prompt_tokens * row_bytes
        ):
            raise ValueError("P contribution cannot hold the prompt KV layout")
        if (
            self.decode_bytes
            < self.layers * self.slots * self.decode_tokens * row_bytes
        ):
            raise ValueError("D contribution cannot hold the decode KV layout")
        if self.stride_bytes < max(self.prompt_bytes, self.decode_bytes):
            raise ValueError("pool stride is smaller than a local contribution")


@dataclass(frozen=True)
class PoolPeer:
    """Identify one worker and its pool across a PD control handshake."""

    session: str
    role: str
    tp_rank: int
    tp_size: int
    pp_size: int
    pool_id: int
    layout: PoolDescriptor

    def __post_init__(self) -> None:
        """Constrain the first demo to its supported fixed-rank topology."""
        _nonempty_string("session", self.session)
        if self.role not in ("prefill", "decode"):
            raise ValueError("role must be prefill or decode")
        _nonnegative_int("TP rank", self.tp_rank)
        if type(self.tp_size) is not int or self.tp_size != 16 or self.tp_rank >= 16:
            raise ValueError("mempool PD requires TP rank in [0, 16)")
        if type(self.pp_size) is not int or self.pp_size != 1:
            raise ValueError("mempool PD requires PP=1")
        _nonnegative_int("pool ID", self.pool_id)
        # Match BM startup's namespace below MF 1.1 TransferEngine entity IDs.
        if self.pool_id >= 256:
            raise ValueError("BM pool ID must be in [0, 256)")
        if not isinstance(self.layout, PoolDescriptor):
            raise ValueError("peer layout is missing")


def validate_peer(local: PoolPeer, remote: PoolPeer) -> None:
    """Require opposite roles and exactly matching paired pool identities."""
    if local.role == remote.role:
        raise ValueError("mempool peers must have opposite PD roles")
    if local.tp_rank != remote.tp_rank or local.tp_size != remote.tp_size:
        raise ValueError("mempool peer TP rank or size differs")
    if local.pp_size != remote.pp_size or local.pool_id != remote.pool_id:
        raise ValueError("mempool peer PP size or pool ID differs")
    if local.layout != remote.layout:
        raise ValueError("mempool peer KV layout or DRAM capacity differs")
    if local.session == remote.session:
        raise ValueError("mempool peers must have distinct startup sessions")


@dataclass(frozen=True)
class RequestIdentity:
    """Tie a bootstrap room to one attempt and both pool startup sessions."""

    room: int
    attempt: str
    p_session: str
    d_session: str

    def __post_init__(self) -> None:
        """Reject missing request identities before they reach slot ownership."""
        _nonnegative_int("bootstrap room", self.room)
        for name in ("attempt", "p_session", "d_session"):
            _nonempty_string(name, getattr(self, name))


@dataclass(frozen=True)
class SlotLease:
    """Identify one use of a physical slot across later slot reuse."""

    slot: int
    generation: int
    proof: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed slot coordinates received over the wire."""
        _nonnegative_int("slot", self.slot)
        _positive_int("slot generation", self.generation)
        if self.proof is not None and (
            not isinstance(self.proof, str)
            or len(self.proof) != 64
            or any(ch not in "0123456789abcdef" for ch in self.proof)
        ):
            raise ValueError("slot proof must be a SHA-256 hex digest")


_REQUIRED: dict[MessageType, frozenset[str]] = {
    MessageType.POOL_HELLO: frozenset(("peer", "reply_to")),
    MessageType.POOL_READY: frozenset(("peer", "receiver_session")),
    MessageType.ACQUIRE: frozenset(
        ("request", "d_slot", "reply_to", "prompt_tokens", "decode_tokens")
    ),
    MessageType.ACQUIRED: frozenset(("request", "p_slot", "d_slot")),
    MessageType.BOUND_ACK: frozenset(("request", "p_slot", "d_slot")),
    MessageType.KV_READY: frozenset(("request", "p_slot", "d_slot", "prompt_tokens")),
    MessageType.CANCEL: frozenset(("request", "d_slot", "reason")),
    MessageType.DONE: frozenset(("request", "p_slot", "d_slot", "reason")),
    MessageType.RELEASE_ACK: frozenset(("request", "p_slot", "d_slot")),
}


@dataclass(frozen=True)
class MempoolMessage:
    """Carry exactly the fields defined for one PD control transition."""

    kind: MessageType
    peer: PoolPeer | None = None
    receiver_session: str | None = None
    request: RequestIdentity | None = None
    p_slot: SlotLease | None = None
    d_slot: SlotLease | None = None
    reply_to: str | None = None
    prompt_tokens: int | None = None
    decode_tokens: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        """Disallow missing or unrelated fields on every message kind."""
        if not isinstance(self.kind, MessageType):
            raise ValueError("unknown mempool message kind")
        present = {
            field.name
            for field in fields(self)
            if field.name != "kind" and getattr(self, field.name) is not None
        }
        required = _REQUIRED[self.kind]
        allowed = required | (
            frozenset(("p_slot", "d_slot"))
            if self.kind == MessageType.CANCEL
            else frozenset()
        )
        if not required <= present or not present <= allowed:
            raise ValueError(f"invalid fields for {self.kind.value} message")
        if self.peer is not None and not isinstance(self.peer, PoolPeer):
            raise ValueError("peer must be a PoolPeer")
        if self.request is not None and not isinstance(self.request, RequestIdentity):
            raise ValueError("request must be a RequestIdentity")
        for name in ("p_slot", "d_slot"):
            slot = getattr(self, name)
            if slot is not None and not isinstance(slot, SlotLease):
                raise ValueError(f"{name} must be a SlotLease")
        for name in ("receiver_session", "reply_to", "reason"):
            value = getattr(self, name)
            if value is not None:
                _nonempty_string(name, value)
        for name in ("prompt_tokens", "decode_tokens"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_int(name, value)


def is_mempool_message(frames: list[bytes]) -> bool:
    """Recognize only this feature's tag on the existing multipart socket."""
    return bool(frames) and frames[0] == WIRE_TAG


def encode_message(message: MempoolMessage) -> list[bytes]:
    """Serialize a validated message into two ZMQ multipart frames."""
    payload = {
        "version": PROTOCOL_VERSION,
        "kind": message.kind.value,
        **{
            name: value
            for name, value in asdict(message).items()
            if value is not None and name != "kind"
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_WIRE_BYTES:
        raise ValueError("mempool control message is too large")
    return [WIRE_TAG, encoded]


_Record = TypeVar("_Record", PoolDescriptor, PoolPeer, RequestIdentity, SlotLease)


def _record(record_type: type[_Record], value: Any) -> _Record:
    """Construct a dataclass only from its exact declared JSON object fields."""
    if not isinstance(value, dict):
        raise ValueError(f"{record_type.__name__} must be a JSON object")
    expected = {field.name for field in fields(record_type)}
    if set(value) != expected:
        raise ValueError(f"{record_type.__name__} has missing or unknown fields")
    if record_type is PoolPeer:
        value = dict(value)
        value["layout"] = _record(PoolDescriptor, value["layout"])
    return record_type(**value)


def decode_message(frames: list[bytes]) -> MempoolMessage:
    """Parse a tagged frame with strict version, shape, and value checks."""
    if len(frames) != 2 or frames[0] != WIRE_TAG or len(frames[1]) > MAX_WIRE_BYTES:
        raise ValueError("invalid mempool multipart frame")
    try:
        payload = json.loads(frames[1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid mempool JSON frame") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != PROTOCOL_VERSION
    ):
        raise ValueError("unsupported mempool protocol version")
    try:
        kind = MessageType(payload.pop("kind"))
    except (KeyError, ValueError) as exc:
        raise ValueError("unknown mempool message kind") from exc
    payload.pop("version")
    required = _REQUIRED[kind]
    allowed = required | (
        frozenset(("p_slot", "d_slot")) if kind == MessageType.CANCEL else frozenset()
    )
    if not required <= payload.keys() or not payload.keys() <= allowed:
        raise ValueError(f"invalid fields for {kind.value} message")
    if "peer" in payload:
        payload["peer"] = _record(PoolPeer, payload["peer"])
    if "request" in payload:
        payload["request"] = _record(RequestIdentity, payload["request"])
    for name in ("p_slot", "d_slot"):
        if name in payload:
            payload[name] = _record(SlotLease, payload[name])
    return MempoolMessage(kind, **payload)
