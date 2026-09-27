"""Logical BF16 KV layout; importing this module needs no NPU libraries."""

from dataclasses import asdict, dataclass
from typing import Any

UINT32_MAX = (1 << 32) - 1
MAX_ROW_BYTES = 32 * 1024
GIB = 1 << 30


def positive_int(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")


def check_index(name: str, value: int, limit: int) -> None:
    if type(value) is not int or not 0 <= value < limit:
        raise IndexError(f"{name} must be an integer in [0, {limit}), got {value}")


@dataclass(frozen=True)
class KVLayout:
    layers: int
    slots: int
    tokens: int
    heads: int
    dim: int
    dtype: str = "bfloat16"

    def __post_init__(self) -> None:
        for name in ("layers", "slots", "tokens", "heads", "dim"):
            positive_int(name, getattr(self, name))
        if self.dtype != "bfloat16":
            raise ValueError("this demo supports only bfloat16 KV")
        if self.row_bytes > MAX_ROW_BYTES:
            raise ValueError("KV row exceeds UniDexCopy's 32 KiB limit")
        if self.layer_bytes > UINT32_MAX:
            raise ValueError("KV layer span exceeds UniDexCopy's UINT32_MAX limit")

    @property
    def shape(self) -> tuple[int, int, int, int]:
        return self.slots, self.tokens, self.heads, self.dim

    @property
    def row_bytes(self) -> int:
        return self.heads * self.dim * 2

    @property
    def layer_bytes(self) -> int:
        return self.slots * self.tokens * self.row_bytes

    @property
    def total_bytes(self) -> int:
        return self.layers * self.layer_bytes

    def row_index(self, slot: int, token: int) -> int:
        check_index("slot", slot, self.slots)
        check_index("token", token, self.tokens)
        return slot * self.tokens + token

    def byte_offset(
        self, layer: int, slot: int, token: int, head: int = 0, column: int = 0
    ) -> int:
        check_index("layer", layer, self.layers)
        check_index("head", head, self.heads)
        check_index("column", column, self.dim)
        return (
            layer * self.layer_bytes
            + self.row_index(slot, token) * self.row_bytes
            + (head * self.dim + column) * 2
        )


@dataclass(frozen=True)
class PoolLayout:
    prompt: KVLayout
    decode: KVLayout
    alignment_bytes: int = GIB

    # A probe lives after all KV slabs, never inside a request's KV.
    probe_bytes: int = 64

    def __post_init__(self) -> None:
        positive_int("alignment_bytes", self.alignment_bytes)
        positive_int("probe_bytes", self.probe_bytes)
        if self.alignment_bytes & (self.alignment_bytes - 1):
            raise ValueError("allocation alignment must be a power of two")
        for name in ("layers", "slots", "heads", "dim", "dtype"):
            if getattr(self.prompt, name) != getattr(self.decode, name):
                raise ValueError(f"P/D KV {name} must match")

    def layout_for_rank(self, rank: int) -> KVLayout:
        check_index("pool rank", rank, 2)
        return (self.prompt, self.decode)[rank]

    def probe_offset(self, rank: int) -> int:
        return self.layout_for_rank(rank).total_bytes

    def contribution_bytes(self, rank: int) -> int:
        required = self.probe_offset(rank) + self.probe_bytes
        return (
            (required + self.alignment_bytes - 1)
            // self.alignment_bytes
            * self.alignment_bytes
        )

    @property
    def rank_stride_bytes(self) -> int:
        return max(self.contribution_bytes(0), self.contribution_bytes(1))

    def signature(self) -> dict[str, Any]:
        return {
            "layout": asdict(self),
            "contributions": [self.contribution_bytes(0), self.contribution_bytes(1)],
            "rank_stride_bytes": self.rank_stride_bytes,
        }
