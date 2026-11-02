"""Recheck Linux TIME_WAIT handling without starting NPU workers or a model."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verify_mempool_session import released_port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as server:
        # Intentionally leave SO_REUSEADDR off, matching the user's Linux repro.
        server.bind(("127.0.0.1", 0))
        server.listen()
        address = server.getsockname()
        with socket.create_connection(address, timeout=3) as client:
            accepted, _ = server.accept()
            with accepted:
                accepted.settimeout(3)
                accepted.shutdown(socket.SHUT_WR)
                if client.recv(1) != b"":
                    raise RuntimeError("client did not observe FIN")
                client.shutdown(socket.SHUT_WR)
                if accepted.recv(1) != b"":
                    raise RuntimeError("server did not observe FIN")
    print(f"Closed {address}; checking reuse for up to {args.timeout}s", flush=True)
    result = released_port(*address, timeout=args.timeout, evidence_path=args.report)
    if not result.get("first_blocked"):
        result.update(status="failed", error="TIME_WAIT wait was not exercised")
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
