"""Observe startup stalls without calling BM/NPU APIs from the sampler thread."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_WAIT_SECONDS = 15.0


def diagnostics_enabled() -> bool:
    """Read the opt-in flag without importing SGLang or an accelerator runtime."""
    return os.environ.get("SGLANG_NPU_MEMPOOL_DIAGNOSTICS", "0") == "1"


def _read(file: Path, limit: int = 65536) -> str:
    try:
        with file.open() as stream:
            return stream.read(limit).strip()
    except OSError as error:
        return f"unavailable({type(error).__name__},errno={error.errno})"


def _fields(file: Path, names: set[str]) -> dict[str, str]:
    content = _read(file)
    if content.startswith("unavailable("):
        return {"read_error": content}
    return {
        key: value.strip()
        for line in content.splitlines()
        for key, separator, value in [line.partition(":")]
        if separator and key in names
    }


def _cgroup_memory(proc: Path) -> dict[str, Any]:
    """Resolve this process's v1/v2 memory hierarchy, including visible ancestors."""
    membership = _read(proc / "cgroup")
    result: dict[str, Any] = {"membership": membership, "visible_limits": {}}
    for line in _read(proc / "mountinfo").splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        mount, fs = before.split(), after.split()
        if len(mount) < 5 or len(fs) < 3:
            continue
        filenames: tuple[str, ...]
        if fs[0] == "cgroup2":
            controller = ""
            filenames = (
                "memory.current",
                "memory.max",
                "memory.high",
                "memory.events",
                "cpuset.mems.effective",
            )
        elif fs[0] == "cgroup" and "memory" in fs[2].split(","):
            controller = "memory"
            filenames = (
                "memory.usage_in_bytes",
                "memory.limit_in_bytes",
                "memory.failcnt",
            )
        else:
            continue

        # mountinfo escapes whitespace/backslashes in paths as octal sequences.
        def unescape(value: str) -> str:
            for escaped, char in (
                (r"\040", " "),
                (r"\011", "\t"),
                (r"\012", "\n"),
                (r"\134", "\\"),
            ):
                value = value.replace(escaped, char)
            return value

        root, mountpoint = Path(unescape(mount[3])), Path(unescape(mount[4]))
        for entry in membership.splitlines():
            parts = entry.split(":", 2)
            if len(parts) != 3 or controller not in parts[1].split(","):
                continue
            member = Path(parts[2])
            if ".." in member.parts or not member.is_relative_to(root):
                continue
            directory = mountpoint / member.relative_to(root)
            while True:
                result["visible_limits"][str(directory)] = {
                    name: _read(directory / name, 4096) for name in filenames
                }
                if directory == mountpoint:
                    break
                directory = directory.parent
    # Ancestors outside the container's cgroup mount cannot be observed here.
    return result


def resource_snapshot(tid: int) -> dict[str, Any]:
    """Read Linux process/host/container evidence; inaccessible files are explicit."""
    proc = Path("/proc/self")
    status = _fields(
        proc / "task" / str(tid) / "status",
        {
            "State",
            "NSpid",
            "VmRSS",
            "VmSize",
            "VmLck",
            "Threads",
            "Cpus_allowed_list",
            "Mems_allowed_list",
        },
    )
    meminfo = _fields(
        Path("/proc/meminfo"),
        {
            "MemTotal",
            "MemFree",
            "MemAvailable",
            "Shmem",
            "SwapFree",
            "HugePages_Total",
            "HugePages_Free",
            "Hugepagesize",
        },
    )
    return {
        "thread_status": status,
        "host_meminfo": meminfo,
        "cgroup": _cgroup_memory(proc),
        "numa_meminfo": {
            file.parent.name: _fields(
                file,
                {
                    f"Node {file.parent.name[4:]} {key}"
                    for key in (
                        "MemTotal",
                        "MemFree",
                        "HugePages_Total",
                        "HugePages_Free",
                    )
                },
            )
            for file in sorted(
                Path("/sys/devices/system/node").glob("node[0-9]*/meminfo")
            )
        },
    }


def _sample(
    context: str, tid: int, *, resources: bool, python_thread: int | None = None
) -> None:
    """Best effort only: unavailable diagnostics must not abort pool startup."""
    try:
        evidence = resource_snapshot(tid) if resources else {}
        task = Path("/proc/self/task") / str(tid)
        evidence["wchan"] = _read(task / "wchan", 1024)
        if python_thread is not None:
            evidence["kernel_stack"] = _read(task / "stack", 4096)
            frame = sys._current_frames().get(python_thread)
            evidence["python_stack"] = (
                [] if frame is None else traceback.format_stack(frame, limit=12)
            )
        logger.info(
            "[MEMPOOL_INIT] SNAPSHOT %s data=%s",
            context,
            json.dumps(evidence, sort_keys=True),
        )
    except Exception as error:
        logger.info("[MEMPOOL_INIT] SNAPSHOT_UNAVAILABLE %s error=%r", context, error)


@contextmanager
def startup_stage(
    stage: str, *, resources: bool = False, **details: Any
) -> Iterator[None]:
    """Log call boundaries and, when opted in, the still-running caller every 15s.

    The monitor only reads procfs/Python frames. It cannot cancel a native call,
    and relies on that call releasing the GIL for Python heartbeat output.
    """
    started = time.monotonic()
    tid = threading.get_native_id()
    context = f"stage={stage} pid={os.getpid()} tid={tid} " + " ".join(
        f"{key}={value}" for key, value in details.items()
    )
    logger.info("[MEMPOOL_INIT] BEGIN %s", context)
    enabled = diagnostics_enabled()
    stop = threading.Event()
    monitor = None
    if enabled:
        if resources:
            _sample(context, tid, resources=True)
        python_thread = threading.get_ident()

        def watch() -> None:
            first = True
            while not stop.wait(_WAIT_SECONDS):
                logger.info(
                    "[MEMPOOL_INIT] WAIT %s elapsed=%.3fs",
                    context,
                    time.monotonic() - started,
                )
                _sample(
                    context,
                    tid,
                    resources=first,
                    python_thread=python_thread if first else None,
                )
                first = False

        monitor = threading.Thread(
            target=watch, name="mempool-startup-watch", daemon=True
        )
        try:
            monitor.start()
        except RuntimeError as error:
            monitor = None
            logger.info(
                "[MEMPOOL_INIT] MONITOR_UNAVAILABLE %s error=%r", context, error
            )
    try:
        yield
    except BaseException as error:
        logger.error(
            "[MEMPOOL_INIT] FAIL %s elapsed=%.3fs error=%r",
            context,
            time.monotonic() - started,
            error,
        )
        raise
    else:
        logger.info(
            "[MEMPOOL_INIT] END %s elapsed=%.3fs", context, time.monotonic() - started
        )
    finally:
        stop.set()
        if monitor is not None:
            monitor.join(timeout=1.0)
        if enabled and resources:
            _sample(context, tid, resources=True)
