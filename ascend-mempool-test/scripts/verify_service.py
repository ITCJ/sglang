"""Check formal P/D server evidence; curl accuracy and performance stay separate."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

RANKS = set(range(16))


def check_order(events: list[str], required: list[str], context: str) -> None:
    if any(event not in events for event in required) or (
        [events.index(event) for event in required]
        != sorted(events.index(event) for event in required)
    ):
        raise RuntimeError(f"incomplete or misordered {context}")


def check_resources(role: str, data: dict[str, Any]) -> None:
    """Check measured allocation/publication sizes, not just the mode flag."""
    if (
        data["mode"] != f"pd_{role}_mempool"
        or data["host_kv_bytes"] != 0
        or data["staging_bytes"] != 0
        or data["transport_staging"]
        or data["registered_main_kv_entries"] != 0
        or data["registered_index_k_entries"] <= 0
        or data["index_k_bytes"] <= 0
        or (role == "prefill" and data["native_kv_bytes"] <= 0)
        or (role == "decode" and data["native_kv_bytes"] != 0)
        or (role == "decode" and data["sparse_cache_bytes"] <= 0)
    ):
        raise RuntimeError(f"unexpected formal {role} resources: {data}")


def check_completion(data: dict[str, Any], layers: int, slots: tuple[int, ...]) -> str:
    """Require graph, binding and validated completion facts before release."""
    steps = data["forwards"]
    if (
        data["cancelled"]
        or not data["drained"]
        or data["layers"] != layers
        or data["layer_checks"] != steps * layers
        or data["submitted_kv"] != steps
        or data["written_kv"] != steps
        or data["replay_forwards"] != steps
    ):
        raise RuntimeError(f"incomplete formal decode: {data}")
    if data["status"] == "zero_decode":
        if steps != 0:
            raise RuntimeError(f"nonempty zero-decode report: {data}")
    elif data["status"] == "completed":
        if (
            steps < 1
            or not isinstance(data["row"], int)
            or data["row"] <= 0
            or (data["prompt_slot"], data["decode_slot"]) != (slots[0], slots[2])
        ):
            raise RuntimeError(f"missing graph or binding evidence: {data}")
    else:
        raise RuntimeError(f"unexpected decode status: {data}")
    return str(data["status"])


def check_logs(
    prefill: list[Path], decode: list[Path], requests: int = 3, *, layers: int = 78
) -> dict[str, Any]:
    """Require complete attempts, every TP rank and physical slot reuse."""
    if requests < 3 or layers < 1:
        raise ValueError("require at least 3 requests and a positive layer count")
    events: dict[tuple[str, int, int, str], list[str]] = {}
    leases: dict[tuple[str, int, int, str], tuple[int, ...]] = {}
    reports: dict[tuple[int, int, str], dict[str, Any]] = {}
    final_free: dict[tuple[str, int], int] = {}
    mapping: dict[str, set[int]] = {role: set() for role in ("prefill", "decode")}
    resources: dict[tuple[str, int], dict[str, Any]] = {}
    native_bytes: dict[tuple[int, str], int] = {}
    generations: dict[tuple[int, str, int], int] = {}
    reused: dict[str, set[int]] = {side: set() for side in ("p", "d")}
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
                        "mempool native transfer rejected",
                        "mempool aux transfer rejected",
                        "mempool state transfer rejected",
                        "mempool KV fetch invalid selection",
                    )
                ):
                    raise RuntimeError(f"failure in {path}: {line}")
                if "mempool " not in line:
                    continue
                fields = dict(
                    re.findall(r"(\w+)=([^\s,]+)", line.split(" data=", 1)[0])
                )
                if role == "decode" and "graph_captured" in line:
                    captured.add(int(fields["device"].split(":")[-1]))
                if role == "decode" and "graph_replay" in line:
                    replayed.add(int(fields["device"].split(":")[-1]))
                if fields.get("role") != role or "rank" not in fields:
                    continue
                rank = int(fields["rank"])
                if "mapping_ready" in line:
                    mapping[role].add(rank)
                if "mempool resources " in line:
                    if (role, rank) in resources:
                        raise RuntimeError("duplicate startup; use fresh server logs")
                    data = json.loads(line.split(" data=", 1)[1])
                    check_resources(role, data)
                    resources[role, rank] = data
                if "mempool native_copy " in line:
                    if int(fields["main_kv_bytes"]) != 0:
                        raise RuntimeError("formal native transport sent main KV")
                    key = (rank, fields["kind"])
                    native_bytes[key] = native_bytes.get(key, 0) + int(fields["bytes"])
                if "free" in fields:
                    final_free[role, rank] = int(fields["free"])
                if int(fields.get("room", -1)) < 0 or "attempt" not in fields:
                    continue
                room, attempt = int(fields["room"]), fields["attempt"]
                identity = (role, rank, room, attempt)
                history = events.setdefault(identity, [])
                event = fields.get("event")
                if "mempool decode_completion " in line:
                    report_key = (rank, room, attempt)
                    if report_key in reports:
                        raise RuntimeError(
                            "duplicate decode completion; use fresh logs"
                        )
                    reports[report_key] = json.loads(line.split(" data=", 1)[1])
                    event = "decode_completion"
                for effect in ("row_detach", "native_free"):
                    if f"mempool {effect} " in line:
                        event = effect
                if event:
                    history.append(event)
                if event in ("ACQUIRED", "start_prefill"):
                    slots = tuple(
                        int(fields[name])
                        for name in ("p_slot", "p_generation", "d_slot", "d_generation")
                    )
                    leases[identity] = slots
                    if role == "decode":
                        for side, slot, generation in (
                            ("p", slots[0], slots[1]),
                            ("d", slots[2], slots[3]),
                        ):
                            slot_key = (rank, side, slot)
                            previous = generations.get(slot_key)
                            if previous is not None:
                                if generation <= previous:
                                    raise RuntimeError(
                                        f"stale reused {side} slot: {identity}"
                                    )
                                reused[side].add(rank)
                            generations[slot_key] = generation
    if any(value != RANKS for value in mapping.values()):
        raise RuntimeError(f"missing mapping_ready on all 16 ranks: {mapping}")
    if captured != RANKS or replayed != RANKS:
        raise RuntimeError("missing capture/replay evidence on all 16 devices")
    for rank in RANKS:
        for role in mapping:
            if (role, rank) not in resources or final_free.get((role, rank)) != 16:
                raise RuntimeError(f"missing resources or free=16: {role} rank={rank}")
        for kind in ("index_k", "aux"):
            if native_bytes.get((rank, kind), 0) <= 0:
                raise RuntimeError(f"missing successful {kind} copy: rank={rank}")
    attempts = {(room, attempt) for _, _, room, attempt in events}
    if len(attempts) < requests:
        raise RuntimeError(
            f"expected at least {requests} attempts, got {len(attempts)}"
        )
    statuses: dict[str, int] = {"completed": 0, "zero_decode": 0}
    for room, attempt in sorted(attempts):
        rank_statuses = set()
        for rank in RANKS:
            p_key, d_key = (
                ("prefill", rank, room, attempt),
                ("decode", rank, room, attempt),
            )
            p_events = events.get(p_key, [])
            d_events = events.get(d_key, [])
            data = reports.get((rank, room, attempt))
            bound_slots = leases.get(d_key)
            if (
                "ready" not in p_events
                or data is None
                or bound_slots is None
                or leases.get(p_key) != bound_slots
            ):
                raise RuntimeError(
                    f"incomplete attempt room={room} attempt={attempt} rank={rank}"
                )
            check_order(
                p_events,
                [
                    "BOUND_ACK",
                    "start_prefill",
                    "row_detach",
                    "native_free",
                    "native_release",
                    "DONE",
                ],
                f"P release room={room} rank={rank}",
            )
            # A completed write receipt survives native detach, so publishing
            # KV_READY may follow native_release in the same TP tick.
            if not (
                p_events.index("start_prefill")
                < p_events.index("ready")
                < p_events.index("DONE")
            ):
                raise RuntimeError(f"misordered P readiness: room={room} rank={rank}")
            rank_statuses.add(check_completion(data, layers, bound_slots))
            ordered = [
                "acquire_decode",
                "ACQUIRED",
                "start_decode",
                "decode_completion",
            ]
            if data["forwards"]:
                ordered.append("row_detach")
            ordered += ["native_free", "release", "RELEASE_ACK"]
            check_order(d_events, ordered, f"D release room={room} rank={rank}")
            # BM readiness and native transfer may arrive in either order.
            if any(
                event not in d_events
                or not d_events.index("ACQUIRED")
                < d_events.index(event)
                < d_events.index("start_decode")
                for event in ("KV_READY", "transfer")
            ):
                raise RuntimeError(
                    f"decode started before joint readiness: room={room} rank={rank}"
                )
        if len(rank_statuses) != 1:
            raise RuntimeError(f"TP ranks disagree on request completion: room={room}")
        statuses[rank_statuses.pop()] += 1
    if statuses["zero_decode"] < 1 or statuses["completed"] < 2:
        raise RuntimeError("require zero-decode and at least two real graph requests")
    if any(ranks != RANKS for ranks in reused.values()):
        raise RuntimeError("missing P/D physical slot reuse with new generations")
    return dict(
        status="formal_service_passed",
        requests=len(attempts),
        ranks_per_side=16,
        zero_decode_requests=statuses["zero_decode"],
        decode_requests=statuses["completed"],
        native_bytes={
            f"rank{rank}_{kind}": count for (rank, kind), count in native_bytes.items()
        },
        resources={f"{role}_{rank}": data for (role, rank), data in resources.items()},
        accuracy="pending user curl check",
        performance="pending measured acceptance",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefill-log", type=Path, nargs="+", required=True)
    parser.add_argument("--decode-log", type=Path, nargs="+", required=True)
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--layers", type=int, default=78)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = check_logs(
            args.prefill_log, args.decode_log, args.requests, layers=args.layers
        )
    except Exception as exc:
        args.report.write_text(
            json.dumps(dict(status="failed", error=str(exc)), indent=2) + "\n"
        )
        raise
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        "FORMAL_SERVICE_PASSED (curl output and performance require separate acceptance)"
    )


if __name__ == "__main__":
    main()
