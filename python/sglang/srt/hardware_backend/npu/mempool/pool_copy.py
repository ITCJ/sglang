"""One persistent SDMA thread per shard; only poll publishes completion."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CopyBlock:
    src_gva: int
    dst_gva: int
    nbytes: int


@dataclass(frozen=True)
class CopyTask:
    rid: str
    op_id: int
    kind: str
    blocks: tuple[CopyBlock, ...]


@dataclass(frozen=True)
class CopyResult:
    task: CopyTask
    state: str
    finished: float = 0
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class Transfer(Protocol):
    def transfer(self, blocks: tuple[CopyBlock, ...]) -> dict[str, Any]: ...


class CompletionUnknown(RuntimeError):
    """The library cannot establish whether all submitted writes finished."""


class BMTransfer:
    """Submit SDK GVAs and wait on the same thread's internal SDMA stream."""

    def __init__(self, handle: Any, bm: Any) -> None:
        self.handle, self.bm = handle, bm

    def transfer(self, blocks: tuple[CopyBlock, ...]) -> dict[str, Any]:
        started = time.monotonic()
        ret = self.handle.copy_data_batch(
            [b.src_gva for b in blocks],
            [b.dst_gva for b in blocks],
            [b.nbytes for b in blocks],
            len(blocks),
            self.bm.BmCopyType.G2G,
            flags=1,
            stream=0,
        )
        submitted = time.monotonic()
        if ret != 0:
            raise RuntimeError(f"BM batch returned {ret}; partial writes possible")
        try:
            ret = self.handle.wait()
        except Exception as exc:  # noqa: BLE001 - deliver thread failures to poll  # noqa: BLE001 - preserve unknown native completion
            raise CompletionUnknown(str(exc)) from exc
        finished = time.monotonic()
        if ret != 0:
            raise CompletionUnknown(f"BM wait returned {ret}; completion unknown")
        return dict(
            submit_enter=started,
            submit_returned=submitted,
            wait_returned=finished,
            submit_return=0,
            wait_return=0,
            tid=threading.get_native_id(),
            blocks=len(blocks),
            bytes=sum(b.nbytes for b in blocks),
        )


class PoolCopyExecutor:
    """Keep library waits off tick; UNKNOWN is terminal even after late success."""

    def __init__(
        self,
        transfer: Transfer,
        *,
        timeout: float,
        setup: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("copy timeout must be positive and finite")
        self.transfer, self.timeout, self.clock = transfer, timeout, clock
        self._tasks: queue.Queue[CopyTask | None] = queue.Queue()
        self._results: queue.Queue[CopyResult] = queue.Queue()
        self._pending: dict[int, tuple[CopyTask, float]] = {}
        self._completed: dict[int, CopyResult] = {}
        self._closed = False
        self._failed = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(setup,), daemon=True)
        self._thread.start()

    def submit(self, task: CopyTask) -> None:
        if self._closed or self._failed.is_set() or task.op_id in self._pending:
            raise RuntimeError("executor closed or duplicate op_id")
        self._pending[task.op_id] = task, self.clock() + self.timeout
        self._tasks.put(task)

    def poll(self, op_id: int) -> CopyResult:
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            if result.task.op_id not in self._completed:
                _, deadline = self._pending[result.task.op_id]
                self._completed[result.task.op_id] = (
                    result
                    if result.finished <= deadline
                    else CopyResult(
                        result.task, "UNKNOWN", error="copy deadline exceeded"
                    )
                )
        task, deadline = self._pending[op_id]
        if op_id not in self._completed and self.clock() >= deadline:
            self._completed[op_id] = CopyResult(
                task, "UNKNOWN", error="copy deadline exceeded"
            )
        result = self._completed.get(op_id, CopyResult(task, "PENDING"))
        if result.state in ("FAILED", "UNKNOWN"):
            self._failed.set()
        return result

    def _run(self, setup: Callable[[], None]) -> None:
        setup_error = ""
        try:
            setup()
        except Exception as exc:  # noqa: BLE001 - deliver thread failures to poll
            setup_error = repr(exc)
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                if (
                    self._failed.is_set()
                    or self.clock() >= self._pending[task.op_id][1]
                ):
                    raise CompletionUnknown("executor stopped or task expired")
                if setup_error:
                    raise RuntimeError(setup_error)
                evidence = self.transfer.transfer(task.blocks) if task.blocks else {}
                result = CopyResult(task, "READY", self.clock(), evidence=evidence)
            except Exception as exc:  # noqa: BLE001 - deliver thread failures to poll
                result = CopyResult(
                    task,
                    "UNKNOWN" if isinstance(exc, CompletionUnknown) else "FAILED",
                    self.clock(),
                    error=repr(exc),
                )
                self._failed.set()
            self._results.put(result)

    def close(self) -> None:
        """Test/owner drain only; never called by scheduler tick on fail-stop."""
        self._closed = True
        self._tasks.put(None)
        self._thread.join(timeout=self.timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                "SDMA thread still active; retain pool until process exit"
            )
