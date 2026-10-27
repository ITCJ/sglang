"""Opt-in, process-local event timestamps for the serial mempool benchmark.

Device completion is observed by nonblocking queries at existing scheduler
polls. No wall clocks, device synchronization, or service policy changes.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PerfTrace:
    def __init__(
        self,
        process: str,
        rank: int = 0,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.process, self.rank, self.clock = process, rank, clock
        self.host, self.pid = socket.gethostname(), os.getpid()
        self.seen: set[tuple[str, str, Any]] = set()
        self.pending: list[tuple[str, Any, str, dict[str, Any]]] = []
        self.rid = ""
        self.room: int | None = None
        self.step = 0

    def mark(self, event: str, rid: str, *, at: int | None = None, **data: Any) -> None:
        if not rid:
            return
        stamp = self.clock() if at is None else at
        logger.info(
            "mempool_perf data=%s",
            json.dumps(
                dict(
                    schema=1,
                    event=event,
                    rid=rid,
                    mono_ns=stamp,
                    host=self.host,
                    pid=self.pid,
                    process=self.process,
                    rank=self.rank,
                    **data,
                ),
                separators=(",", ":"),
            ),
        )

    def once(self, event: str, rid: str, **data: Any) -> None:
        key = (rid, event, data.get("op_id"))
        if key not in self.seen:
            self.seen.add(key)
            self.mark(event, rid, **data)

    def begin(self, rid: str, room: int) -> None:
        if self.rid != rid:
            self.poll()
            if self.pending:
                raise RuntimeError("perf completion belongs to an unfinished request")
            self.seen.clear()
            self.rid, self.room, self.step = rid, room, 0

    def device(self, event: str, completion: Any, **data: Any) -> None:
        self.pending.append((event, completion, self.rid, data))
        self.mark(event + "_submitted", self.rid, **data)

    def poll(self) -> None:
        remaining = []
        for event, completion, rid, data in self.pending:
            if completion.query():
                self.mark(event, rid, **data)
            else:
                remaining.append((event, completion, rid, data))
        self.pending = remaining

    def output_sent(self, payload: Any) -> None:
        for index, rid in enumerate(payload.rids):
            if rid == self.rid:
                ids = payload.output_ids[index]
                if ids:
                    self.once("first_token_sent", rid, output_tokens=len(ids))
