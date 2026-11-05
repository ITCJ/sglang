"""Ticket14 measurement plan. Token counts describe newly computed KV per layer."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.page_write import PageWriter
else:
    from .page_write import PageWriter

PRODUCERS = (
    0,
    1,
    2,
    6,
    10,
    14,
    18,
    22,
    26,
    30,
    34,
    38,
    42,
    46,
    50,
    54,
    58,
    62,
    66,
    70,
    74,
)


@dataclass(frozen=True)
class BenchmarkConfig:
    page_size: int = 64
    tp_size: int = 16
    nodes: tuple[int, ...] = tuple(range(8))
    layers: int = 78
    capacity: int = 131072
    samples: int = 3
    iterations: int = 100
    warmup: int = 3
    suite: str = "full"
    seed: int = 14
    row_bytes: int = 1152
    copy_backend: str = "sdk"

    @property
    def groups(self) -> int:
        return len(self.nodes)

    @property
    def page_layer_bytes(self) -> int:
        return self.page_size * self.row_bytes

    @property
    def page_bytes(self) -> int:
        return self.page_layer_bytes * self.layers

    def signature(self) -> dict[str, Any]:
        return dict(asdict(self), nodes=list(self.nodes))


@dataclass(frozen=True)
class Case:
    name: str
    group: str
    operation: str = "read"
    side: int = 0
    location: str = "local"
    readers: int = 1
    grain: str = "token"
    tokens: int = 32768
    striped: bool = False
    background: str = ""
    targets: str = "remote"
    slice_bytes: int = 0
    pipeline: bool = False

    def logical_bytes(self, config: BenchmarkConfig) -> int:
        return self.tokens * config.row_bytes

    def block_bytes(self, config: BenchmarkConfig) -> int:
        return (
            self.slice_bytes
            or {
                "token": config.row_bytes,
                "layer": config.page_layer_bytes,
                "page": config.page_bytes,
            }[self.grain]
        )

    def read_blocks(self, config: BenchmarkConfig) -> int:
        return (
            2048
            if self.group == "small_read"
            else math.ceil(self.logical_bytes(config) / self.block_bytes(config))
        )


def build_plan(config: BenchmarkConfig) -> list[Case]:
    """A bounded set of experiments, not a Cartesian product of every knob."""
    lengths = (8192,) if config.suite == "smoke" else (4096, 8192, 32768, 131072)
    result = [
        Case(
            f"write-{n}-{target}{'-dread' if busy else ''}",
            "remote_write",
            operation="write",
            tokens=n,
            targets=target,
            background="read" if busy else "",
        )
        for n in lengths
        for target, busy in (
            ("local", False),
            ("remote", False),
            ("dual", False),
            ("remote", True),
            ("dual", True),
        )
    ]
    result.append(
        Case(
            "d-local-read-baseline", "interference_read", side=1, readers=config.tp_size
        )
    )
    result += [
        Case(
            "pipeline-8192-dual",
            "pipeline",
            operation="write",
            tokens=8192,
            targets="dual",
            pipeline=True,
        )
    ]
    if config.suite == "remote-write":
        return result
    counts = (
        (1,)
        if config.suite == "smoke"
        else tuple(n for n in (1, 2, 4, 8, 16) if n <= config.tp_size)
    )
    for side in (0, 1):
        for location in ("local", "remote"):
            for grain in ("token", "layer", "page"):
                for n in counts:
                    result.append(
                        Case(
                            f"read-{side}-{location}-{grain}-{n}",
                            "readers",
                            side=side,
                            location=location,
                            readers=n,
                            grain=grain,
                        )
                    )
                result.append(
                    Case(
                        f"striped-{side}-{location}-{grain}",
                        "readers",
                        side=side,
                        location=location,
                        readers=config.tp_size,
                        grain=grain,
                        striped=True,
                    )
                )
        if config.groups > 1:
            for operation in ("read", "write"):
                result.append(
                    Case(
                        f"other-{side}-{operation}",
                        "locations",
                        operation=operation,
                        side=side,
                        location="other",
                        tokens=8192,
                    )
                )
        for location in ("local", "remote"):
            result.append(
                Case(
                    f"bulk-{side}-{location}",
                    "locations",
                    operation="write",
                    side=side,
                    location=location,
                    tokens=8192,
                )
            )
        for n in counts:
            result.append(
                Case(
                    f"writeflow-{side}-{n}",
                    "write_flow",
                    side=side,
                    readers=n,
                    background="write",
                )
            )
        for size in (1152, 4096, 16384, 147456):
            result.append(
                Case(
                    f"small-{side}-{size}",
                    "small_read",
                    side=side,
                    location="remote",
                    slice_bytes=size,
                )
            )
    return result


def write_pages(config: BenchmarkConfig, case: Case, rank: int) -> list[int]:
    result: list[int] = PageWriter(rank, config.tp_size, config.page_size).pages(
        case.tokens
    )
    return result
