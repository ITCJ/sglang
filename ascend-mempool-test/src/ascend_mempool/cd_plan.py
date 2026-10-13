"""Fixed-shape TP1/2/4 bulk-write and sparse-read experiment contract."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Any

from .sharing import SharingPlan

TOKENS, SLOTS, DIM, TOPK, BLOCK_DIM = 65536, 16, 576, 2048, 48
WRITE_ROWS = (2048, 8192, 32768)


@dataclass(frozen=True)
class Topology:
    name: str
    tp_size: int
    mode: str
    nodes: tuple[int, ...]

    def signature(self) -> dict[str, Any]:
        return dict(
            name=self.name, tp_size=self.tp_size, mode=self.mode, nodes=list(self.nodes)
        )

    def plan(self) -> SharingPlan:
        return SharingPlan(TOKENS, TOKENS, SLOTS, 1, DIM, self.tp_size)


def topologies(nodes: list[int]) -> list[Topology]:
    if (
        len(nodes) != 4
        or len(set(nodes)) != 4
        or any(type(n) is not int or not 0 <= n <= 126 for n in nodes)
    ):
        raise ValueError("need four distinct online memory nodes in [0,126]")
    result = [Topology("1-I0", 1, "independent", (nodes[0],))]
    for n in (2, 4):
        result.extend(
            [
                Topology(f"{n}-I0", n, "independent", (nodes[0],) * n),
                Topology(f"{n}-S0", n, "shared", (nodes[0],) * n),
                Topology(f"{n}-ID", n, "independent", tuple(nodes[:n])),
            ]
        )
    return result


def schedule(nodes: list[int], rounds: int) -> list[tuple[int, Topology]]:
    choices = topologies(nodes)
    result: list[tuple[int, Topology]] = []
    for round_id in range(rounds):
        sizes = [1, 2, 4]
        sizes = sizes[round_id % 3 :] + sizes[: round_id % 3]
        for n in sizes:
            group = [t for t in choices if t.tp_size == n]
            offset = round_id % len(group)
            result.extend((round_id, t) for t in group[offset:] + group[:offset])
    return result


@dataclass(frozen=True)
class CDCase:
    name: str
    operation: str
    rows: int = 0
    active: int = 1
    source: str = "local"
    distribution: str = "random"

    def signature(self) -> dict[str, Any]:
        return asdict(self)


def cases(
    tp_size: int, sources: list[str], *, smoke: bool = False, contiguous: bool = False
) -> list[CDCase]:
    result = [
        CDCase(f"write-{kind}-{n}", kind, rows=n)
        for kind in (["logical"] if tp_size == 1 else ["logical", "equal_total"])
        for n in ((2048,) if smoke else WRITE_ROWS)
    ]
    result += [
        CDCase(
            f"read-{source}-{distribution}-{active}",
            "read",
            active=active,
            source=source,
            distribution=distribution,
        )
        for source in sources
        for distribution in (["random", "contiguous"] if contiguous else ["random"])
        for active in ((1,) if smoke else (1, 16))
    ]
    return result


def write_span(
    rows: int, member: int, tp_size: int, mode: str, operation: str
) -> tuple[int, int]:
    if rows < 1 or not 0 <= member < tp_size or tp_size not in (1, 2, 4):
        raise ValueError("invalid write partition")
    if operation not in ("logical", "equal_total") or mode not in (
        "independent",
        "shared",
    ):
        raise ValueError("unknown write experiment")
    if mode == "independent" and operation == "logical":
        return 0, rows
    # Contiguous, balanced spans, including odd correctness-only tails.
    count, tail = divmod(rows, tp_size)
    begin = member * count + min(member, tail)
    return begin, begin + count + (member < tail)


def indices(
    case: CDCase, variant: int, seed: int, tokens: int = TOKENS, topk: int = TOPK
) -> list[list[int]]:
    if tokens <= topk or case.active < 1:
        raise ValueError("history must exceed the full effective top-k")
    result = []
    for request in range(case.active):
        rng = random.Random(seed + variant * 100003 + request * 1009)
        if case.distribution == "contiguous":
            first = rng.randrange(tokens - topk + 1)
            result.append(list(range(first, first + topk)))
        else:
            result.append(rng.sample(range(tokens), topk))
    return result


def cpu_partitions(available: set[int]) -> list[list[int]]:
    if len(available) < 4:
        raise ValueError("the fixed CPU node needs at least four allowed CPUs")
    cpus = sorted(available)
    return [cpus[member::4] for member in range(4)]


def expected_bytes(
    case: CDCase, tp_size: int, mode: str, member: int
) -> tuple[int, int]:
    if case.operation == "read":
        count = case.active * TOPK * DIM * 2
        return count, count
    begin, end = write_span(case.rows, member, tp_size, mode, case.operation)
    return (end - begin) * DIM * 2, case.rows * DIM * 2
