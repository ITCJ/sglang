"""Multi-worker rendezvous for the standalone experiment, outside device timing."""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from typing import Any, Callable

from .control import TestChannel

CLEANUP_PHASES = {
    "drained": "drained",
    "pools-closed": "closed",
    "clients-uninitialized": "uninitialized",
}


class CohortServer:
    """Collect phases; after failure permit only coordinated resource teardown."""

    def __init__(
        self, host: str, port: int, identity: str, timeout: float, tp_size: int = 2
    ) -> None:
        if tp_size not in (1, 2, 4):
            raise ValueError("tp_size must be 1, 2 or 4")
        self.world_size = 2 * tp_size
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
                            or rank not in range(owner.world_size)
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
                        if message["phase"] == "clients-uninitialized" or (
                            message["phase"] == "drained"
                            and not message["payload"].get("keep_open", False)
                        ):
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
        cleanup = phase in CLEANUP_PHASES
        key = phase if cleanup else str(seq)
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
            while len(entries) < self.world_size:
                if self.error and not cleanup:
                    return self.error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.error = f"timeout waiting for all workers: {phase}"
                    self.condition.notify_all()
                    return self.error
                self.condition.wait(min(1.0, remaining))
            if cleanup:
                field = CLEANUP_PHASES[phase]
                failed = {
                    rank: m["payload"].get("error", f"{field}=false")
                    for rank, m in entries.items()
                    if not m["payload"].get(field)
                }
                if failed:
                    return f"cleanup {phase} failed: {failed}"
            elif self.error:
                return self.error
            return [entries[i]["payload"] for i in range(self.world_size)]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Cohort:
    def __init__(
        self,
        host: str,
        port: int,
        rank: int,
        identity: str,
        timeout: float,
        tp_size: int = 2,
    ) -> None:
        if tp_size not in (1, 2, 4) or rank not in range(2 * tp_size):
            raise ValueError("invalid TP size or cohort rank")
        self.tp_size = tp_size
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection = socket.create_connection(
                    (host, port), timeout=min(2, timeout)
                )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("worker coordinator was not reachable") from None
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

    def shutdown(
        self,
        *,
        drain: Callable[[], None],
        close_pool: Callable[[], None],
        uninitialize: Callable[[], None],
        store_owner: bool,
        success: bool,
        progress: Callable[[str], None] = lambda phase: None,
    ) -> bool:
        """Finish the worker's device and SDK resources as one cohort."""

        def perform(phase: str, action: Callable[[], None]) -> str | None:
            progress(phase)
            try:
                action()
            except BaseException as exc:
                return f"{type(exc).__name__}: {exc}"
            return None

        error = perform("device-drain", drain)
        outcomes = self.exchange(
            "drained",
            drained=error is None,
            error=error,
            success=success and error is None,
            keep_open=True,
        )
        success = success and all(outcome["success"] for outcome in outcomes)
        error = perform("pool-close", close_pool)
        progress("waiting-all-pools-closed")
        self.exchange("pools-closed", closed=error is None, error=error)
        # A BM Store is part of its rank's SDK context. Keep it alive through
        # every leave/destroy AND every non-Store client's uninitialization.
        error = None if store_owner else perform("client-uninitialize", uninitialize)
        progress("waiting-all-clients-uninitialized")
        self.exchange("clients-uninitialized", uninitialized=error is None, error=error)
        if store_owner:
            progress("store-uninitialize")
            uninitialize()
        progress("closed")
        return success

    def aligned_start(self, phase: str) -> None:
        self.exchange(phase + "/ready")
        stamps = self.exchange(phase + "/clock", ns=time.monotonic_ns())
        offset = (self.rank // self.tp_size) * self.tp_size
        local = stamps[offset : offset + self.tp_size]
        target = max(s["ns"] for s in local) + 100_000_000
        if time.monotonic_ns() >= target:
            # This deadline is a launch hint, not a validity check. Inactive
            # peers must still reach /done after a delayed control reply.
            # Active peers launch now; measured start skew and overlap decide
            # sample admission (and the C/D caller retries invalid batches).
            return
        remaining = (target - time.monotonic_ns()) / 1e9
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
        while time.monotonic_ns() < target:
            pass

    def close(self) -> None:
        self.channel.close()
