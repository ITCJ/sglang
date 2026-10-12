"""Four-worker rendezvous for the standalone experiment, outside device timing."""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from typing import Any

from .control import TestChannel


class CohortServer:
    """Collect identical phases; on failure allow a final all-worker drain only."""

    def __init__(self, host: str, port: int, identity: str, timeout: float) -> None:
        self.identity, self.timeout = identity, timeout
        self.condition = threading.Condition()
        self.phases: dict[str, dict[int, dict[str, Any]]] = {}
        self.connected: set[int] = set()
        self.error: str | None = None
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                channel = TestChannel(self.request)
                self.request.settimeout(timeout)
                try:
                    hello = channel.expect("hello")
                    rank = hello.get("rank")
                    with owner.condition:
                        if (
                            hello.get("identity") != identity
                            or type(rank) is not int
                            or rank not in range(4)
                            or rank in owner.connected
                        ):
                            raise ValueError(
                                "worker identity mismatch or duplicate rank"
                            )
                        owner.connected.add(rank)
                    channel.send("hello_ok")
                    while True:
                        message = channel.expect("exchange")
                        result = owner.collect(rank, message)
                        if isinstance(result, str):
                            channel.send("abort", error=result)
                        else:
                            channel.send("result", values=result)
                        if message["phase"] == "drained":
                            break
                except Exception as exc:
                    with owner.condition:
                        owner.error = str(exc)
                        owner.condition.notify_all()
                finally:
                    channel.close()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def collect(self, rank: int, message: dict[str, Any]) -> list[Any] | str:
        phase, seq = message["phase"], message["seq"]
        key = "drained" if phase == "drained" else str(seq)
        deadline = time.monotonic() + self.timeout
        with self.condition:
            entries = self.phases.setdefault(key, {})
            if rank in entries:
                self.error = "worker repeated an exchange"
            entries[rank] = message
            if phase == "drained" and not message["payload"].get("success", True):
                self.error = "worker failed; stopping submissions before final drain"
            if len({m["phase"] for m in entries.values()}) != 1:
                self.error = f"workers disagree at phase {seq}"
            self.condition.notify_all()
            while len(entries) < 4:
                if self.error and phase != "drained":
                    return self.error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.error = f"timeout waiting for all workers: {phase}"
                    self.condition.notify_all()
                    return self.error
                self.condition.wait(min(1.0, remaining))
            if phase == "drained":
                if not all(m["payload"].get("drained") for m in entries.values()):
                    return "one or more devices did not drain"
            elif self.error:
                return self.error
            return [entries[i]["payload"] for i in range(4)]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Cohort:
    def __init__(
        self, host: str, port: int, rank: int, identity: str, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection = socket.create_connection(
                    (host, port), timeout=min(2, timeout)
                )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "four-worker coordinator was not reachable"
                    ) from None
                time.sleep(0.1)
        connection.settimeout(timeout)
        self.channel = TestChannel(connection)
        self.seq = 0
        self.rank = rank
        self.channel.send("hello", rank=rank, identity=identity)
        self.channel.expect("hello_ok")

    def exchange(self, phase: str, **payload: Any) -> list[Any]:
        self.channel.send("exchange", seq=self.seq, phase=phase, payload=payload)
        self.seq += 1
        return list(self.channel.expect("result")["values"])

    def aligned_start(self, phase: str) -> None:
        self.exchange(phase + "/ready")
        stamps = self.exchange(phase + "/clock", ns=time.monotonic_ns())
        local = stamps[(self.rank // 2) * 2 : (self.rank // 2) * 2 + 2]
        target = max(s["ns"] for s in local) + 100_000_000
        if time.monotonic_ns() >= target:
            raise RuntimeError("coordination exceeded 100ms launch window")
        remaining = (target - time.monotonic_ns()) / 1e9
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        while time.monotonic_ns() < target:
            pass

    def close(self) -> None:
        self.channel.close()
