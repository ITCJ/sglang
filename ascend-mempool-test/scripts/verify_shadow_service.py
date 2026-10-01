"""Send a small shadow-service gate through the existing PD router, or check logs."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any


def request_gate(url: str, output: Path, timeout: float, decode_tokens: int) -> None:
    """Exercise first-token finish, graph decode and subsequent request reuse."""
    if decode_tokens < 2:
        raise ValueError("--decode-tokens must be at least 2 to exercise decode")
    cases = (
        ("zero_decode", "Reply with the word hello.", 1),
        ("decode", "Briefly explain why the sky is blue.", decode_tokens),
        ("reuse", "Briefly explain why ice floats on water.", decode_tokens),
    )
    report: dict[str, Any] = {"status": "running", "url": url, "checks": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for name, prompt, limit in cases:
            body = {
                "text": prompt,
                "sampling_params": {
                    "temperature": 0,
                    "max_new_tokens": limit,
                    "ignore_eos": True,
                },
                "stream": False,
            }
            request = urllib.request.Request(
                url.rstrip("/") + "/generate",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise RuntimeError(f"{name}: unexpected response {result!r}")
            metadata = result.get("meta_info", {})
            reason = metadata.get("finish_reason", {})
            if result.get("error") or reason.get("type") == "abort":
                raise RuntimeError(f"{name}: request aborted: {result!r}")
            if metadata.get("completion_tokens") != limit:
                raise RuntimeError(
                    f"{name}: expected {limit} output tokens, got {metadata!r}"
                )
            report["checks"].append(
                {
                    "case": name,
                    "seconds": time.monotonic() - started,
                    "request": body,
                    "response": result,
                }
            )
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(f"PASS case={name} completion_tokens={limit}", flush=True)
        report["status"] = "requests_passed"
        print(
            "REQUESTS_PASSED: wait for release ACKs, then run check-logs; inspect generated text.",
            flush=True,
        )
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def check_logs(
    prefill: list[Path], decode: list[Path], requests: int
) -> dict[str, Any]:
    """Require every TP rank's normal lifecycle and actual graph evidence."""
    events: dict[tuple[str, int, int], set[str]] = {}
    final_free: dict[tuple[str, int], int] = {}
    mapping: dict[str, set[int]] = {"prefill": set(), "decode": set()}
    captured: set[int] = set()
    replayed: set[int] = set()
    for role, paths in (("prefill", prefill), ("decode", decode)):
        for path in paths:
            for line in path.read_text(errors="replace").splitlines():
                if any(
                    marker in line
                    for marker in (
                        "Traceback (most recent call last)",
                        "mempool TP tick failed",
                        "Ascend mempool control fault",
                        "watchdog expired",
                        "mempool valid write counts differ",
                        "native transfer drain unconfirmed",
                    )
                ):
                    raise RuntimeError(f"failure in {path}: {line}")
                if "mempool " not in line:
                    continue
                fields = dict(re.findall(r"(\w+)=([^\s,]+)", line))
                if "graph_captured" in line:
                    captured.add(int(fields["device"].split(":")[-1]))
                if "graph_replay" in line:
                    replayed.add(int(fields["device"].split(":")[-1]))
                if fields.get("role") != role or "rank" not in fields:
                    continue
                rank = int(fields["rank"])
                if "mapping_ready" in line:
                    mapping[role].add(rank)
                if "event" not in fields or "room" not in fields:
                    continue
                room = int(fields["room"])
                if room >= 0:
                    events.setdefault((role, rank, room), set()).add(fields["event"])
                final_free[role, rank] = int(fields["free"])
    ranks = set(range(16))
    if any(value != ranks for value in mapping.values()):
        raise RuntimeError(
            f"mapping_ready must cover all 16 ranks on both sides: {mapping}"
        )
    if captured != ranks or replayed != ranks:
        raise RuntimeError(
            f"missing per-device Graph evidence: capture={captured}, replay={replayed}"
        )
    expected = {
        "prefill": {"start_prefill", "BOUND_ACK", "ready", "native_release", "DONE"},
        "decode": {
            "acquire_decode",
            "ACQUIRED",
            "start_decode",
            "release",
            "RELEASE_ACK",
        },
    }
    rooms = sorted(
        {room for role, rank, room in events if role == "decode" and rank == 0}
    )
    if len(rooms) < requests:
        raise RuntimeError(
            f"expected at least {requests} real requests; found {len(rooms)}"
        )
    for room in rooms:
        for role, required in expected.items():
            for rank in ranks:
                missing = required - events.get((role, rank, room), set())
                if missing:
                    raise RuntimeError(
                        f"room={room} role={role} rank={rank} missing={sorted(missing)}"
                    )
    if any(final_free.get((role, rank)) != 16 for role in expected for rank in ranks):
        raise RuntimeError(
            "not all ranks have returned to 16 available slots; wait for ACK or inspect logs"
        )
    return {
        "status": "shadow_lifecycle_passed",
        "rooms": rooms,
        "ranks_per_side": 16,
        "readback": "not run",
        "accuracy": "inspect output; full model evaluation deferred",
    }


def main() -> None:
    """Keep traffic generation and offline lifecycle verification explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "requests", help="Send three sequential requests to an existing PD router"
    )
    run.add_argument("--url", required=True, help="Existing PD router base URL")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--decode-tokens", type=int, default=32)
    logs = commands.add_parser(
        "check-logs", help="Check full P/D logs after the request gate"
    )
    logs.add_argument("--prefill-logs", type=Path, nargs="+", required=True)
    logs.add_argument("--decode-logs", type=Path, nargs="+", required=True)
    logs.add_argument("--requests", type=int, default=3)
    logs.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "requests":
        request_gate(args.url, args.output, args.timeout, args.decode_tokens)
    else:
        result = check_logs(args.prefill_logs, args.decode_logs, args.requests)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print("SHADOW_LIFECYCLE_PASSED (no KV readback)")


if __name__ == "__main__":
    main()
