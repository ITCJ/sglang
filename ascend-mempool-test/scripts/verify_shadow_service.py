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
    prefill: list[Path],
    decode: list[Path],
    requests: int,
    *,
    require_readback: bool = False,
    readback_layers: int = 78,
) -> dict[str, Any]:
    """Require every TP rank's normal lifecycle and actual graph evidence."""
    events: dict[tuple[str, int, int], set[str]] = {}
    final_free: dict[tuple[str, int], int] = {}
    mapping: dict[str, set[int]] = {"prefill": set(), "decode": set()}
    captured: set[int] = set()
    replayed: set[int] = set()
    readback: dict[tuple[int, int], dict[str, Any]] = {}
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
                        "mempool KV readback mismatch",
                        "mempool KV readback coverage mismatch",
                        "mempool KV readback failed",
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
                if role == "decode" and "mempool readback_result " in line:
                    room = int(fields["room"])
                    key = (rank, room)
                    if key in readback:
                        raise RuntimeError(
                            f"duplicate readback result rank={rank} room={room}; use fresh logs"
                        )
                    readback[key] = {
                        "rank": rank,
                        "room": room,
                        "attempt": fields["attempt"],
                        "data": json.loads(line.split(" data=", 1)[1]),
                    }
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
    numerical: Any = "not run"
    if require_readback:
        numerical = check_readback(readback, rooms, readback_layers)
    return {
        "status": "shadow_readback_passed"
        if require_readback
        else "shadow_lifecycle_passed",
        "rooms": rooms,
        "ranks_per_side": 16,
        "readback": numerical,
        "accuracy": "inspect output; full model evaluation deferred",
    }


def check_readback(
    reports: dict[tuple[int, int], dict[str, Any]],
    rooms: list[int],
    layers: int,
) -> dict[str, Any]:
    """Require completed real comparisons for every worker and both decode requests."""
    if layers < 1:
        raise ValueError("--readback-layers must be positive")
    zero_rooms: list[int] = []
    decode_rooms: list[int] = []
    for room in rooms:
        statuses = set()
        for rank in range(16):
            context = f"readback rank={rank} room={room}"
            entry = reports.get((rank, room))
            if entry is None:
                raise RuntimeError(f"missing {context}")
            data = entry["data"]
            steps = data["forwards"]
            if entry["attempt"] == "NONE" or data["cancelled"]:
                raise RuntimeError(f"{context}: missing attempt or cancelled request")
            if data["layers"] != layers or data["layer_checks"] != layers * steps:
                raise RuntimeError(f"{context}: incomplete layer coverage")
            if data["written_kv"] != steps:
                raise RuntimeError(f"{context}: readback/write forward counts differ")
            statuses.add(data["status"])
            if data["status"] == "zero_decode":
                if any(
                    data[name] != 0
                    for name in (
                        "forwards",
                        "replay_forwards",
                        "prompt_kv",
                        "decode_kv",
                        "prompt_boundary_kv",
                        "decode_first_kv",
                        "max_topk",
                    )
                ):
                    raise RuntimeError(
                        f"{context}: zero-decode request has KV evidence"
                    )
            elif data["status"] == "passed":
                if steps < 1 or not 0 < data["replay_forwards"] <= steps:
                    raise RuntimeError(f"{context}: missing real readback Graph replay")
                if data["min_topk"] != 2048 or data["max_topk"] != 2048:
                    raise RuntimeError(f"{context}: expected top-k width 2048")
                if any(
                    data[name] is None or data[name] <= 0
                    for name in (
                        "prompt_kv",
                        "decode_kv",
                        "prompt_boundary_kv",
                        "decode_first_kv",
                        "min_valid_per_layer",
                    )
                ):
                    raise RuntimeError(
                        f"{context}: missing valid P/D KV or boundary evidence"
                    )
            else:
                raise RuntimeError(f"{context}: comparison did not pass")
        if len(statuses) != 1:
            raise RuntimeError(
                f"readback room={room}: workers disagree about zero-decode"
            )
        (zero_rooms if "zero_decode" in statuses else decode_rooms).append(room)
    if not zero_rooms or len(decode_rooms) < 2:
        raise RuntimeError(
            "readback requires zero-decode plus two real decode/reuse requests"
        )
    return {
        "status": "passed",
        "zero_decode_rooms": zero_rooms,
        "decode_rooms": decode_rooms,
        "reuse": check_readback_reuse(reports, decode_rooms),
        "reports": [reports[key] for key in sorted(reports)],
        "scope": "valid selected KV at width 2048; not 2048 valid tokens or full-capacity coverage",
    }


def check_readback_reuse(
    reports: dict[tuple[int, int], dict[str, Any]], rooms: list[int]
) -> list[dict[str, Any]]:
    """Require actual row and P/D slot reuse across distinct completed attempts."""
    evidence: list[dict[str, Any]] = []
    names = ("row", "prompt_slot", "decode_slot")
    for rank in range(16):
        seen: dict[tuple[int, ...], tuple[int, str]] = {}
        reused = None
        for room in rooms:
            entry = reports[rank, room]
            data = entry["data"]
            for name in names:
                value = data.get(name)
                lower = 1 if name == "row" else 0
                upper = 16 if name == "row" else 15
                if type(value) is not int or not lower <= value <= upper:
                    raise RuntimeError(
                        f"readback rank={rank} room={room}: invalid attachment {name}={value}"
                    )
            attachment = tuple(data[name] for name in names)
            previous = seen.get(attachment)
            if previous is not None and previous[1] != entry["attempt"]:
                reused = {
                    "rank": rank,
                    "rooms": [previous[0], room],
                    "attempts": [previous[1], entry["attempt"]],
                    **dict(zip(names, attachment)),
                }
            seen[attachment] = (room, entry["attempt"])
        if reused is None:
            raise RuntimeError(
                f"readback rank={rank}: no actual row/P/D slot reuse across decode attempts; "
                "wait for all RELEASE_ACKs, send the requests again, and retain this run's logs"
            )
        evidence.append(reused)
    return evidence


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
    logs.add_argument("--require-readback", action="store_true")
    logs.add_argument(
        "--readback-layers",
        type=int,
        default=78,
        help="Expected local model layers (GLM-5.1: 78)",
    )
    logs.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "requests":
        request_gate(args.url, args.output, args.timeout, args.decode_tokens)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = check_logs(
                args.prefill_logs,
                args.decode_logs,
                args.requests,
                require_readback=args.require_readback,
                readback_layers=args.readback_layers,
            )
        except Exception as exc:
            args.output.write_text(
                json.dumps({"status": "failed", "error": str(exc)}, indent=2) + "\n"
            )
            raise
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(
            "SHADOW_READBACK_PASSED"
            if args.require_readback
            else "SHADOW_LIFECYCLE_PASSED (no KV readback)"
        )


if __name__ == "__main__":
    main()
