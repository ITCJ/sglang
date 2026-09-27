"""Bounded test-only TCP framing; this is not the SGLang PD control protocol."""

import json
import socket
from typing import Any

MAX_FRAME_BYTES = 65536


class TestChannel:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self._stream = connection.makefile("rwb")

    def send(self, kind: str, **payload: Any) -> None:
        frame = json.dumps({"kind": kind, "payload": payload}).encode("utf-8") + b"\n"
        if len(frame) > MAX_FRAME_BYTES:
            raise ValueError("test control frame is too large")
        self._stream.write(frame)
        self._stream.flush()

    def receive(self) -> tuple[str, dict[str, Any]]:
        frame = self._stream.readline(MAX_FRAME_BYTES + 1)
        if not frame:
            raise ConnectionError("test peer closed before acknowledgement")
        if len(frame) > MAX_FRAME_BYTES or not frame.endswith(b"\n"):
            raise ValueError("test control frame is oversized or incomplete")
        message = json.loads(frame)
        if not isinstance(message, dict) or not isinstance(message.get("kind"), str):
            raise ValueError("test control frame has no valid message kind")
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("test control payload must be an object")
        return message["kind"], payload

    def expect(self, kind: str) -> dict[str, Any]:
        actual, payload = self.receive()
        if actual != kind:
            raise RuntimeError(f"expected {kind}, got {actual}: {payload}")
        return payload

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self.connection.close()
