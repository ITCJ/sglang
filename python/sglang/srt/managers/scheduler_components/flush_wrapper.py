import logging
import time
from typing import Callable, Optional, Tuple

from sglang.srt.managers.io_struct import (
    FlushCacheReqInput,
    FlushCacheReqOutput,
    WaitUntilIdleReqInput,
)
from sglang.srt.managers.scheduler_components.ipc_channels import (
    SchedulerIpcChannels,
)


class SchedulerFlushWrapper:
    def __init__(
        self,
        *,
        flush_cache: Callable[[], bool],
        is_fully_idle: Callable[[], bool],
        ipc_channels: SchedulerIpcChannels,
    ) -> None:
        self._flush_cache = flush_cache
        self._is_fully_idle = is_fully_idle
        self._ipc_channels = ipc_channels
        self._pending: Optional[Tuple[FlushCacheReqInput, float]] = None

    def handle(self, recv_req: FlushCacheReqInput) -> Optional[FlushCacheReqOutput]:
        wait_only = isinstance(recv_req, WaitUntilIdleReqInput)
        if self._pending is not None:
            return FlushCacheReqOutput(
                success=False,
                message=(
                    "Another cache idle operation is already in progress."
                    if wait_only
                    else "Another flush_cache is already in progress."
                ),
            )

        timeout_s = float(recv_req.timeout_s or 0.0)
        if wait_only:
            if self._is_fully_idle():
                return FlushCacheReqOutput(success=True)
            if timeout_s <= 0.0:
                return FlushCacheReqOutput(
                    success=False, message="Scheduler is not idle."
                )
            self._pending = (recv_req, time.monotonic() + timeout_s)
            return None

        if timeout_s <= 0.0:
            return FlushCacheReqOutput(success=self._flush_cache())

        if self._is_fully_idle():
            return FlushCacheReqOutput(success=self._flush_cache())

        self._pending = (recv_req, time.monotonic() + timeout_s)
        return None

    def check_pending(self) -> None:
        if self._pending is None:
            return

        pending_req, deadline = self._pending
        wait_only = isinstance(pending_req, WaitUntilIdleReqInput)
        timed_out = wait_only and time.monotonic() >= deadline

        if not timed_out and self._is_fully_idle():
            success = True if wait_only else self._flush_cache()
            self._pending = None
            self._ipc_channels.send_to_tokenizer.send_output(
                FlushCacheReqOutput(success=success), pending_req
            )
            return

        if timed_out or time.monotonic() >= deadline:
            operation = "wait_until_idle" if wait_only else "flush_cache"
            logging.warning(
                "Deferred %s timed out while waiting for idle state.", operation
            )
            self._pending = None
            self._ipc_channels.send_to_tokenizer.send_output(
                FlushCacheReqOutput(
                    success=False, message="Timed out waiting for idle state."
                ),
                pending_req,
            )
