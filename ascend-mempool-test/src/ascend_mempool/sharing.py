"""CPU-readable contract for the two-member NUMA sharing experiment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.layout import KVLayout, PoolLayout
else:
    from .layout import KVLayout, PoolLayout


@dataclass(frozen=True)
class SharingPlan:
    prompt_tokens: int = 8192
    decode_tokens: int = 4096
    slots: int = 2
    layers: int = 2
    dim: int = 576

    def layout(self) -> PoolLayout:
        return PoolLayout(
            KVLayout(self.layers, self.slots, self.prompt_tokens, 1, self.dim),
            KVLayout(self.layers, self.slots, self.decode_tokens, 1, self.dim),
            probe_bytes=256,
        )

    def participants(self, mode: str) -> list[int]:
        if mode not in ("independent", "shared"):
            raise ValueError("mode must be independent or shared")
        return list(range(4 if mode == "shared" else 2))

    def contributions(self, mode: str) -> list[int]:
        self.participants(mode)
        layout = self.layout()
        p, d = (layout.contribution_bytes(side) for side in (0, 1))
        return [p, 0, d, 0] if mode == "shared" else [p, p, d, d]

    def owner(self, mode: str, *, side: int, member: int) -> int:
        self.participants(mode)
        if side not in (0, 1) or member not in (0, 1):
            raise ValueError("side and member must be 0 or 1")
        return side * 2 if mode == "shared" else side


def port_plan(base: int, mode: str, side: int, member: int) -> dict[str, int]:
    """MF binds NIC base + BM rank; reserve offsets 0..12 on each host."""
    SharingPlan().owner(mode, side=side, member=member)
    if not 1 <= base <= 65523:
        raise ValueError("control-port must leave thirteen consecutive ports available")
    rank = side * 2 + member if mode == "shared" else side
    nic = base + 4 + 4 * member
    return dict(
        coordinator=base + 1,
        store=base + 2 + (member if mode == "independent" else 0),
        nic_base=nic,
        nic_actual=nic + rank,
        collective=base + 12,
    )


def writer_rows(mode: str, experiment: str, rows: int, member: int) -> list[int]:
    """Physical row offsets; equal-bytes uses disjoint ranges in the shared pool."""
    if mode not in ("independent", "shared") or member not in (0, 1):
        raise ValueError("invalid pool mode or member")
    if (
        experiment not in ("logical", "equal_bytes")
        or type(rows) is not int
        or rows < 1
    ):
        raise ValueError("invalid write experiment or row count")
    if mode == "independent":
        return list(range(rows))
    if experiment == "equal_bytes":
        return list(range(member * rows, (member + 1) * rows))
    return list(range(member, rows, 2))


def group_sample(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine only two members on ONE host; never subtract P and D clocks."""
    if len(samples) != 2 or {s["member"] for s in samples} != {0, 1}:
        raise ValueError("a group sample needs both local members")
    if len({s["repeats"] for s in samples}) != 1:
        raise ValueError("members must submit the same repeat count")
    start = min(s["start_ns"] for s in samples)
    end = max(s["done_ns"] for s in samples)
    skew = max(s["start_ns"] for s in samples) - start
    if end <= start or any(s["done_ns"] <= s["start_ns"] for s in samples):
        raise ValueError("invalid host completion interval")
    elapsed_ms = (end - start) / 1e6
    payload = sum(s["copy_bytes"] for s in samples) * samples[0]["repeats"]
    logical = max(s["logical_bytes"] for s in samples) * samples[0]["repeats"]
    bm_payload = (
        sum(s.get("bm_copy_bytes", s["copy_bytes"]) for s in samples)
        * samples[0]["repeats"]
    )
    return dict(
        host_envelope_ms=elapsed_ms,
        launch_skew_ms=skew / 1e6,
        aligned_batches=(
            max(s["start_ns"] for s in samples) < min(s["done_ns"] for s in samples)
            and skew / 1e6 <= min(s["device_ms"] for s in samples) * 0.1
        ),
        device_ms=[s["device_ms"] for s in sorted(samples, key=lambda s: s["member"])],
        copy_bytes=payload,
        bm_copy_bytes=bm_payload,
        logical_bytes=logical,
        copy_payload_gbps=payload / (elapsed_ms * 1e6) if payload else None,
        bm_payload_gbps=bm_payload / (elapsed_ms * 1e6) if bm_payload else None,
        logical_gbps=logical / (elapsed_ms * 1e6) if logical else None,
        bus_traffic_bytes=None,
    )
