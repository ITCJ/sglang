"""Opt-in process-local JSON lines; no device imports or protocol ownership."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


def write_evidence(record: dict[str, Any], folder: Path) -> Path:
    """Retain correctness evidence independently of the performance switch."""
    payload = json.dumps(record, sort_keys=True)
    relative = Path("prefix-content") / (
        hashlib.sha256(payload.encode()).hexdigest() + ".json"
    )
    destination = folder / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload + "\n")
    return relative


class Observation:
    def __init__(
        self,
        root: Path,
        run_id: str,
        role: str,
        rank: int,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        folder = root / "observe"
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{role}-rank{rank:02d}-{os.getpid()}.jsonl"
        self._file = self.path.open("a", buffering=65536)
        self._lock = threading.Lock()
        self.clock = clock
        host = socket.gethostname()
        self.identity = dict(
            schema_version=1,
            run_id=run_id,
            host=host,
            machine=host,
            pid=os.getpid(),
            role=role,
            rank=rank,
            attn_group=0,
            clock_domain=f"host_monotonic:{host}",
        )

    def emit(self, event: str, **values: Any) -> None:
        record = dict(self.identity, mono_ns=self.clock(), event=event, **values)
        with self._lock:
            self._file.write(json.dumps(record, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


def create_observation(
    config: Any,
    run_id: str,
    role: str,
    rank: int,
    *,
    root: Optional[Path] = None,
) -> Optional[Observation]:
    if not config.observe:
        return None
    if root is None:
        root = Path(os.environ["SGLANG_NPU_MEMPOOL_EVIDENCE_DIR"])
    return Observation(root, run_id, role, rank)
