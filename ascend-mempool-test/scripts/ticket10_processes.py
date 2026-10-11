"""Local child ownership and bounded peer messages for the paired test runner."""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

MAX_FRAME = 65536


class Peer:
    """A test-only channel; messages select fixed phases, never shell commands."""

    def __init__(self, connection: socket.socket, timeout: float) -> None:
        self.connection = connection
        self.timeout = timeout
        self.inbox: queue.Queue[Any] = queue.Queue(maxsize=16)
        self.pending: deque[dict[str, Any]] = deque()
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            with self.connection.makefile("rb") as stream:
                while not self.closed.is_set():
                    data = stream.readline(MAX_FRAME + 1)
                    if not data:
                        raise ConnectionError("peer disconnected")
                    if len(data) > MAX_FRAME or not data.endswith(b"\n"):
                        raise ValueError("oversized or incomplete peer frame")
                    message = json.loads(data)
                    if not isinstance(message, dict) or not isinstance(
                        message.get("kind"), str
                    ):
                        raise ValueError("invalid peer frame")
                    self._put(message)
        except Exception as exc:
            self._put(exc)

    def _put(self, value: Any) -> None:
        while not self.closed.is_set():
            try:
                self.inbox.put(value, timeout=0.2)
                return
            except queue.Full:
                pass

    def send(self, kind: str, **payload: Any) -> None:
        data = json.dumps(dict(kind=kind, **payload)).encode() + b"\n"
        if len(data) > MAX_FRAME:
            raise ValueError("peer frame exceeds limit")
        self.connection.sendall(data)

    def check(self) -> None:
        """Detect peer failure while a local child runs; keep early barriers."""
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                return
            if isinstance(message, Exception):
                raise message
            if message["kind"] == "error":
                raise RuntimeError(f"peer failed: {message.get('message')}")
            self.pending.append(message)
            return

    def expect(
        self, kind: str, check: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while not self.pending:
            check()
            self.check()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for peer {kind}")
            if not self.pending:
                time.sleep(0.1)
        message = self.pending.popleft()
        if message.pop("kind") != kind:
            raise RuntimeError(f"unexpected peer phase; expected {kind}: {message}")
        return message

    def barrier(self, phase: str) -> None:
        self.send("barrier", phase=phase)
        if self.expect("barrier") != dict(phase=phase):
            raise RuntimeError(f"peer phase differs: {phase}")

    def __enter__(self) -> Peer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()
        self.reader.join(timeout=1)


class Processes:
    """Own fresh process groups; never stop an unrelated service on the machine."""

    def __init__(self, root: Path, env: Optional[dict[str, str]] = None) -> None:
        self.root = root
        self.env = env
        self.children: list[subprocess.Popen[bytes]] = []

    def start(
        self, name: str, command: list[str], **environment: str
    ) -> subprocess.Popen[bytes]:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{name}.command.json").write_text(
            json.dumps(command, indent=2) + "\n"
        )
        env = dict(os.environ if self.env is None else self.env, **environment)
        with (self.root / f"{name}.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        self.children.append(process)
        print(
            f"START {name} pid={process.pid} log={self.root / (name + '.log')}",
            flush=True,
        )
        return process

    def wait(
        self,
        name: str,
        child: subprocess.Popen[bytes],
        *,
        timeout: float,
        check: Callable[[], None] = lambda: None,
    ) -> None:
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            check()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{name} exceeded {timeout}s; see {self.root}")
            time.sleep(0.2)
        if child.returncode:
            raise RuntimeError(
                f"{name} exited with {child.returncode}; see {self.root}"
            )

    @staticmethod
    def alive(name: str, child: subprocess.Popen[bytes]) -> None:
        if child.poll() is not None:
            raise RuntimeError(f"{name} exited unexpectedly with {child.returncode}")

    def stop(self) -> None:
        deadline = time.monotonic() + 30
        owned = {child.pid for child in self.children}
        live = set(owned)
        try:
            # A shell leader may exit before its TP workers; signal its whole group.
            for child in reversed(self.children):
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            while live and time.monotonic() < deadline:
                for child in self.children:
                    child.poll()
                # Ignore already-dead zombies, but wait for every live group member.
                table = subprocess.check_output(["ps", "-eo", "pgid=,stat="], text=True)
                live = {
                    int(parts[0])
                    for row in table.splitlines()
                    if len(parts := row.split()) == 2
                    and int(parts[0]) in owned
                    and not parts[1].startswith("Z")
                }
                if live:
                    time.sleep(0.2)
        finally:
            # Even a failed process-table check must reap our children.
            try:
                for group in live:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                for child in self.children:
                    child.wait(timeout=10)
                self.children.clear()
        if live:
            raise RuntimeError(
                f"owned process groups required forced stop: {sorted(live)}"
            )

    def __enter__(self) -> Processes:
        return self

    def __exit__(self, *args: Any) -> None:
        try:
            self.stop()
        except Exception as exc:
            if args[0] is None:
                raise
            print(f"Cleanup also failed: {exc}", file=sys.stderr, flush=True)
