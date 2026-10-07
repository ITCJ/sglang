"""Logical BF16 KV layout; importing this module needs no NPU libraries."""

from typing import Any

import msgspec

UINT32_MAX = (1 << 32) - 1
MAX_ROW_BYTES = 32 * 1024
GIB = 1 << 30
_ELEMENT_BYTES_BY_DTYPE = {"bfloat16": 2}


def positive_int(name: str, value: int) -> None:
    """Require a positive integer, excluding bool and implicit float conversions."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")


def check_index(name: str, value: int, limit: int) -> None:
    """Reject a logical coordinate outside its axis before producing an address."""
    if type(value) is not int or not 0 <= value < limit:
        raise IndexError(f"{name} must be an integer in [0, {limit}), got {value}")


class KVLayout(msgspec.Struct, frozen=True):
    """Describe contiguous BF16 layer slabs with [slot, token, head, dim] axes."""

    layers: int
    slots: int
    tokens: int
    heads: int
    dim: int
    dtype: str = "bfloat16"

    def __post_init__(self) -> None:
        """Validate dimensions and the current UniDexCopy per-layer limits."""
        for name in ("layers", "slots", "tokens", "heads", "dim"):
            positive_int(name, getattr(self, name))
        if self.dtype not in _ELEMENT_BYTES_BY_DTYPE:
            raise ValueError("this demo supports only bfloat16 KV")
        if self.row_bytes > MAX_ROW_BYTES:
            raise ValueError("KV row exceeds UniDexCopy's 32 KiB limit")
        if self.layer_bytes > UINT32_MAX:
            raise ValueError("KV layer span exceeds UniDexCopy's UINT32_MAX limit")

    @property
    def shape(self) -> tuple[int, int, int, int]:
        """Return one layer's logical tensor shape."""
        return self.slots, self.tokens, self.heads, self.dim

    @property
    def element_bytes(self) -> int:
        """Return the byte width of the layout's validated KV dtype."""
        return _ELEMENT_BYTES_BY_DTYPE[self.dtype]

    @property
    def row_bytes(self) -> int:
        """Return the compact KV bytes belonging to one token."""
        return self.heads * self.dim * self.element_bytes

    @property
    def layer_bytes(self) -> int:
        """Return one layer's addressable span across all slots and tokens."""
        return self.slots * self.tokens * self.row_bytes

    @property
    def total_bytes(self) -> int:
        """Return the KV payload size before probe space and allocation alignment."""
        return self.layers * self.layer_bytes

    def row_index(self, slot: int, token: int) -> int:
        """Map a slot/token pair to its UniDexCopy row within one layer."""
        check_index("slot", slot, self.slots)
        check_index("token", token, self.tokens)
        return slot * self.tokens + token

    def byte_offset(
        self, layer: int, slot: int, token: int, head: int = 0, column: int = 0
    ) -> int:
        """Map a checked logical element to its byte offset within a rank."""
        check_index("layer", layer, self.layers)
        check_index("head", head, self.heads)
        check_index("column", column, self.dim)
        return (
            layer * self.layer_bytes
            + self.row_index(slot, token) * self.row_bytes
            + (head * self.dim + column) * self.element_bytes
        )


class PoolLayout(msgspec.Struct, frozen=True):
    """Describe P rank 0 and D rank 1 contributions with a shared GVA stride."""

    prompt: KVLayout
    decode: KVLayout
    alignment_bytes: int = GIB

    # A probe lives after all KV slabs, never inside a request's KV.
    probe_bytes: int = 64

    def __post_init__(self) -> None:
        """Require compatible layer schemas and a power-of-two allocation alignment."""
        positive_int("alignment_bytes", self.alignment_bytes)
        positive_int("probe_bytes", self.probe_bytes)
        if self.alignment_bytes & (self.alignment_bytes - 1):
            raise ValueError("allocation alignment must be a power of two")
        for name in ("layers", "slots", "heads", "dim", "dtype"):
            if getattr(self.prompt, name) != getattr(self.decode, name):
                raise ValueError(f"P/D KV {name} must match")

    def layout_for_rank(self, rank: int) -> KVLayout:
        """Select prompt or decode storage using the two-rank pool's local rank."""
        check_index("pool rank", rank, 2)
        return (self.prompt, self.decode)[rank]

    def probe_offset(self, rank: int) -> int:
        """Locate the reserved mapping probe after the rank's KV payload."""
        return self.layout_for_rank(rank).total_bytes

    def contribution_bytes(self, rank: int) -> int:
        """Round this rank's KV payload and probe up to the backend alignment."""
        required = self.probe_offset(rank) + self.probe_bytes
        return (
            (required + self.alignment_bytes - 1)
            // self.alignment_bytes
            * self.alignment_bytes
        )

    @property
    def rank_stride_bytes(self) -> int:
        """Return the common create2 maximum without inflating local contributions."""
        return max(self.contribution_bytes(0), self.contribution_bytes(1))

    def signature(self) -> dict[str, Any]:
        """Return serializable layout metadata for peer compatibility checks."""
        return {
            "layout": msgspec.to_builtins(self),
            "contributions": [self.contribution_bytes(0), self.contribution_bytes(1)],
            "rank_stride_bytes": self.rank_stride_bytes,
        }
