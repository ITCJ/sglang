"""Parent-only progress display: never add I/O inside NPU timing windows."""

from __future__ import annotations

import time
from pathlib import Path


class Progress:
    def __init__(self, total_groups: int, total_cases: int) -> None:
        self.total_groups, self.total_cases = total_groups, total_cases
        self.started = time.monotonic()
        self.last_print = float("-inf")
        self.done = 0
        self.offsets = [0, 0]
        self.counts = [0, 0]
        self.labels = ["setup / staging", "setup / staging"]
        self.phase = ""
        self.stage = Path()

    def begin(self, phase: str, stage: Path) -> None:
        self.phase, self.stage = phase, stage
        self.offsets, self.counts = [0, 0], [0, 0]
        self.labels = ["setup / staging", "setup / staging"]
        self.last_print = float("-inf")
        self.update()

    def update(self, *, finished: bool = False, reused: bool = False) -> None:
        now = time.monotonic()
        if not finished and now - self.last_print < 10:
            return
        self.last_print = now
        for member in (0, 1):
            path = self.stage / f"member-{member}.log"
            if not path.exists():
                continue
            with path.open(errors="replace") as stream:
                stream.seek(self.offsets[member])
                while True:
                    before = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith("\n"):
                        self.offsets[member] = before
                        break
                    if line.startswith("BEGIN "):
                        self.labels[member] = line[6:].strip()
                    elif line.startswith("PASS "):
                        self.counts[member] += 1
        completed = min(self.counts)
        fraction = min(completed / max(1, self.total_cases), 0.99)
        if finished:
            self.done += 1
            fraction = 0
        ratio = (self.done + fraction) / self.total_groups
        filled = int(24 * ratio)
        bar = "#" * filled + "-" * (24 - filled)
        state = (
            "REUSED"
            if reused
            else "DONE"
            if finished
            else (
                f"cases {completed}/{self.total_cases} | "
                + " | ".join(f"m{m}: {self.labels[m]}" for m in (0, 1))
            )
        )
        print(
            f"[{bar}] {ratio:6.1%} groups {self.done}/{self.total_groups} "
            f"{self.phase} | {state} | elapsed {now - self.started:.0f}s",
            flush=True,
        )
