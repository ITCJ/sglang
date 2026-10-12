"""Representative experiments, with the same logical work in both pool modes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Case:
    name: str
    operation: str
    rows: int = 1
    active: int = 1
    source: str = "mixed"
    distribution: str = "scattered"
    partition: str = "token"
    hit_percent: int = 0
    accessor: int = -1
    layer: int = 0

    def signature(self) -> dict[str, Any]:
        return asdict(self)


def cases(smoke: bool = False, diagnostic: bool = False) -> list[Case]:
    if diagnostic:
        if smoke:
            raise ValueError("write/read diagnostic requires eager and Graph")
        return [
            Case(f"write_then_read-{active}", "write_then_read", active=active)
            for active in (1, 16)
        ]
    if smoke:
        return [Case("write-tail", "logical", rows=17), Case("mixed-read", "read")]
    result = [
        Case(f"{op}-{rows}-{partition}", op, rows=rows, partition=partition)
        for op in ("logical", "equal_bytes")
        for rows in (1, 17, 2049)
        for partition in (
            ("token", "feature", "layer") if op == "logical" else ("token",)
        )
    ]
    result += [
        Case(f"logical-{rows}-layer1", "logical", rows=rows, partition="layer", layer=1)
        for rows in (1, 17, 2049)
    ]
    result += [
        Case(
            f"read-{source}-{distribution}-{active}",
            "read",
            active=active,
            source=source,
            distribution=distribution,
        )
        for source in ("p", "d", "mixed")
        for distribution in ("contiguous", "scattered")
        for active in (1, 16)
    ]
    result += [
        Case(
            f"{op}-single-{member}",
            op,
            rows=17 if op == "logical" else 1,
            accessor=member,
        )
        for op in ("logical", "read")
        for member in (0, 1)
    ]
    result += [
        Case(f"{op}-{active}", op, active=active)
        for op in (
            "write_then_read",
            "write_with_history",
            "current_serial",
            "current_parallel",
        )
        for active in (1, 16)
    ]
    result += [
        Case(f"materialize-{hit}", "materialize", active=16, hit_percent=hit)
        for hit in (0, 50, 100)
    ]
    result.append(Case("zero-valid", "read", active=0))
    return result
