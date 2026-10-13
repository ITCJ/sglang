"""Progress across variable-width TP groups, including case-level resume."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from cd_results import save


class CDProgress:
    def __init__(self, root: Path, total: int) -> None:
        self.root, self.total = root, total
        self.started = time.monotonic()
        self.completed = 0
        self.phase = ""
        self.last_poll = self.last_print = 0.0
        self.units = 0
        self.logs: list[Path] = []
        self.offsets: list[int] = []
        self.counts: list[int] = []
        self.labels: list[str] = []

    def begin(self, phase: str, stage: Path, members: int, units: int) -> None:
        self.phase, self.units = phase, units
        self.logs = [stage / f"member-{m}.log" for m in range(members)]
        self.offsets, self.counts = [0] * members, [0] * members
        self.labels = ["setup / mapping / staging"] * members
        self.last_poll = self.last_print = 0.0
        self.update()

    def snapshot(self) -> dict[str, Any]:
        done = self.completed + min(self.units, min(self.counts, default=0))
        elapsed = time.monotonic() - self.started
        slowest = min(range(len(self.counts)), key=self.counts.__getitem__)
        return dict(
            phase=self.phase,
            current=self.labels[slowest],
            completed=done,
            total=self.total,
            elapsed_s=round(elapsed, 1),
            eta_s=round(elapsed / done * (self.total - done), 1) if done else None,
        )

    def update(self, *, finished: bool = False) -> None:
        now = time.monotonic()
        if not finished and now - self.last_poll < 1:
            return
        self.last_poll = now
        for member, path in enumerate(self.logs):
            if not path.exists():
                continue
            with path.open(errors="replace") as stream:
                stream.seek(self.offsets[member])
                for line in stream:
                    if line.startswith("BEGIN "):
                        self.labels[member] = line.strip()[6:]
                    elif line.startswith("PASS "):
                        self.counts[member] += 1
                self.offsets[member] = stream.tell()
        if finished:
            self.completed += self.units
            self.units = 0
            self.labels = ["closed"] * len(self.logs)
        if finished or now - self.last_print >= 10:
            state = self.snapshot()
            save(self.root / "progress.json", state)
            filled = int(24 * state["completed"] / self.total)
            bar = "#" * filled + "-" * (24 - filled)
            eta = (
                "estimating"
                if state["eta_s"] is None
                else f"{state['eta_s'] / 60:.1f}m"
            )
            print(
                f"[{bar}] {state['completed']}/{self.total} "
                f"{self.phase} | {state['current']} | "
                f"elapsed {state['elapsed_s'] / 60:.1f}m ETA {eta}",
                flush=True,
            )
            self.last_print = now
