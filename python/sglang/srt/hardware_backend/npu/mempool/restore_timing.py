"""Temporary K1-03 acceptance timing; remove after user NPU acceptance."""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator

import torch


class RestoreTiming:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.started = time.monotonic_ns()
        self.events: list[tuple[str, Any, Any]] = []

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start, end = (torch.npu.Event(enable_timing=True) for _ in range(2))
        start.record()
        yield
        end.record()
        self.events.append((name, start, end))

    def finish(self) -> dict[str, float]:
        """Called only after the restore completion event has finished."""
        if not self.enabled:
            return {}
        result: dict[str, float] = defaultdict(float)
        for name, start, end in self.events:
            result[name] += start.elapsed_time(end)
        result["T_lb_rank"] = (time.monotonic_ns() - self.started) / 1e6
        return dict(result)
