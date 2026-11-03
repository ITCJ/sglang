"""One persistent SDMA thread per shard; only poll publishes completion."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


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


class Transfer(Protocol):
    def transfer(self, blocks: tuple[CopyBlock, ...]) -> None: ...


class CompletionUnknown(RuntimeError):
    """The library cannot establish whether all submitted writes finished."""


class BMTransfer:
    """Submit SDK GVAs and wait on the same thread's internal SDMA stream."""

    def __init__(self, handle: Any, bm: Any) -> None:
        self.handle, self.bm = handle, bm
        self.observer: Any = None
        self.observation_task: CopyTask | None = None

    def _observe(self, event: str) -> None:
        if self.observer is not None and self.observation_task is not None:
            task = self.observation_task
            self.observer.emit(event, rid=task.rid, op_id=task.op_id, kind=task.kind)

    def transfer(self, blocks: tuple[CopyBlock, ...]) -> None:
        if self.observer is not None:
            self._observe("library_submit_begin")
        ret = self.handle.copy_data_batch(
            [b.src_gva for b in blocks],
            [b.dst_gva for b in blocks],
            [b.nbytes for b in blocks],
            len(blocks),
            self.bm.BmCopyType.G2G,
            flags=1,
            stream=0,
        )
        if self.observer is not None:
            self._observe("library_submit_return")
        if ret != 0:
            raise RuntimeError(f"BM batch returned {ret}; partial writes possible")
        try:
            ret = self.handle.wait()
        except Exception as exc:  # noqa: BLE001 - deliver thread failures to poll  # noqa: BLE001 - preserve unknown native completion
            raise CompletionUnknown(str(exc)) from exc
        if ret != 0:
            raise CompletionUnknown(f"BM wait returned {ret}; completion unknown")
        if self.observer is not None:
            self._observe("library_complete")


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
        self.observer: Any = None
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
        if self.observer is not None:
            self.observer.emit(
                "executor_enqueue",
                rid=task.rid,
                op_id=task.op_id,
                kind=task.kind,
                block_count=len(task.blocks),
                nbytes=sum(b.nbytes for b in task.blocks),
                zero_work=not task.blocks,
            )
        self._tasks.put(task)

    def poll(self, op_id: int) -> CopyResult:
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            if result.task.op_id not in self._completed:
                if self.observer is not None:
                    self.observer.emit(
                        "copy_poll_observed",
                        rid=result.task.rid,
                        op_id=result.task.op_id,
                        kind=result.task.kind,
                        state=result.state,
                    )
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
            logger.exception("pool copy executor setup failed")
            setup_error = repr(exc)
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                if self.observer is not None:
                    self.observer.emit(
                        "executor_start", rid=task.rid, op_id=task.op_id, kind=task.kind
                    )
                    if isinstance(self.transfer, BMTransfer):
                        self.transfer.observation_task = task
                if (
                    self._failed.is_set()
                    or self.clock() >= self._pending[task.op_id][1]
                ):
                    raise CompletionUnknown("executor stopped or task expired")
                if setup_error:
                    raise RuntimeError(setup_error)
                if task.blocks:
                    self.transfer.transfer(task.blocks)
                result = CopyResult(task, "READY", self.clock())
            except Exception as exc:  # noqa: BLE001 - deliver thread failures to poll
                result = CopyResult(
                    task,
                    "UNKNOWN" if isinstance(exc, CompletionUnknown) else "FAILED",
                    self.clock(),
                    error=repr(exc),
                )
                self._failed.set()
            self._results.put(result)

    def retire(self, op_id: int) -> None:
        """Drop completed request records after the caller's source-release gate."""
        if self._completed[op_id].state != "READY":
            raise RuntimeError("cannot retire an incomplete or failed copy")
        del self._completed[op_id]
        del self._pending[op_id]

    def close(self) -> None:
        """Test/owner drain only; never called by scheduler tick on fail-stop."""
        self._closed = True
        self._tasks.put(None)
        self._thread.join(timeout=self.timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                "SDMA thread still active; retain pool until process exit"
            )


def device_thread_setup(device: str, numa_node: int, bind_cpu: bool = True) -> None:
    """Bind only the copy thread, preserving scheduler affinity."""
    import os
    from pathlib import Path

    import torch

    torch.npu.set_device(device)
    if bind_cpu:
        text = (
            Path(f"/sys/devices/system/node/node{numa_node}/cpulist")
            .read_text()
            .strip()
        )
        cpus: set[int] = set()
        for part in text.split(","):
            limits = [int(value) for value in part.split("-")]
            cpus.update(range(limits[0], limits[-1] + 1))
        # The inherited scheduler mask is not the container's allowed cpuset.
        # Let Linux filter the target NUMA CPUs through the actual cpuset.
        os.sched_setaffinity(0, cpus)
