"""Analyze ticket13 JSON lines and device MSTX ranges without loading a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

from observation_legacy_mstx import recover_ranges


def records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    result = []
    for path in paths:
        files = path.rglob("*.jsonl") if path.is_dir() else [path]
        for file in files:
            with file.open() as stream:
                for line in stream:
                    record = json.loads(line)
                    if record["schema_version"] != 1:
                        raise ValueError(f"unsupported schema in {file}")
                    result.append(record)
    prompts = {
        (r["run_id"], r["rid"], r["host"], r["role"], r["rank"]): r["page_hashes"]
        for r in result
        if r["event"] == "request_tracked"
    }
    for r in result:
        if r["event"] == "layer_pages" and "page_hashes" not in r:
            r["page_hashes"] = prompts[
                (r["run_id"], r["rid"], r["host"], r["role"], r["rank"])
            ]
    return result


def ratio(top: int, bottom: int) -> float | None:
    return top / bottom if bottom else None


def miss_rate(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[int]] = defaultdict(lambda: [0] * 6)
    for r in data:
        if r["event"] != "layer_pages":
            continue
        sums = [
            sum(p[k] for p in r["pages"])
            for k in ("selected_count", "miss_count", "io_miss_count")
        ]
        if sums != [r[k] for k in ("selected_count", "miss_count", "io_miss_count")]:
            raise ValueError("online/page counts disagree")
        if any(
            p["selected_count"] != p["hit_count"] + p["miss_count"]
            or p["miss_count"] != p["io_miss_count"] + p["current_device_supply_count"]
            for p in r["pages"]
        ):
            raise ValueError("page hit/miss/supply counts disagree")
        key = (
            r["page_size_tokens"],
            r["hot_cache_tokens"],
            r["replacement_policy"],
            r["layer_id"],
        )
        row = groups[key]
        for i, number in enumerate(sums):
            row[i] += number
        row[3] += len(r["pages"])
        row[4] += sum(p["miss_count"] > 0 for p in r["pages"])
        row[5] += 1
    return [
        dict(
            page_size_tokens=k[0],
            hot_cache_tokens=k[1],
            replacement_policy=k[2],
            layer=k[3],
            n=v[5],
            selected=v[0],
            cache_miss=v[1],
            io_miss=v[2],
            token_cache_miss_rate=ratio(v[1], v[0]),
            io_miss_rate=ratio(v[2], v[0]),
            page_miss_rate=ratio(v[4], v[3]),
        )
        for k, v in sorted(groups.items())
    ]


def concentration(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, list[int]]] = defaultdict(dict)
    for r in data:
        if r["event"] != "layer_pages":
            continue
        key = (
            r["machine"],
            r["reader_numa"],
            r["rank"],
            r["layer_id"],
            r["hot_cache_tokens"],
            r["page_size_tokens"],
            r["replacement_policy"],
        )
        for p in r["pages"]:
            counts = groups[key].setdefault(p["content_page"], [0, 0])
            counts[0] += p["io_miss_count"]
            counts[1] += sum(s["nbytes"] for s in p["sources"])
    output = []
    for key, pages in sorted(groups.items()):
        for col, unit in enumerate(("rows", "bytes")):
            values = sorted((c[col] for c in pages.values()), reverse=True)
            output.append(
                dict(
                    machine=key[0],
                    reader_numa=key[1],
                    rank=key[2],
                    layer=key[3],
                    hot_cache_tokens=key[4],
                    page_size_tokens=key[5],
                    replacement_policy=key[6],
                    unit=unit,
                    pages=len(values),
                    total=sum(values),
                    curve=[
                        dict(
                            page_fraction=f,
                            miss_fraction=ratio(
                                sum(values[: math.ceil(len(values) * f)]), sum(values)
                            ),
                        )
                        for f in (0.01, 0.05, 0.1, 0.2, 0.5, 1.0)
                    ],
                )
            )
    return output


def cross_step(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[int, set[str]]] = defaultdict(dict)
    for r in data:
        if r["event"] == "layer_pages":
            key = (
                r["run_id"],
                r["rid"],
                r["rank"],
                r["layer_id"],
                r["host"],
                r["page_size_tokens"],
                r["hot_cache_tokens"],
                r["replacement_policy"],
            )
            groups[key][r["step"]] = {p["content_page"] for p in r["pages"]}
    return [
        dict(
            run_id=k[0],
            rid=k[1],
            rank=k[2],
            layer=k[3],
            host=k[4],
            page_size_tokens=k[5],
            hot_cache_tokens=k[6],
            replacement_policy=k[7],
            step=step,
            overlap=ratio(len(current & steps[step - 1]), len(current)),
        )
        for k, steps in groups.items()
        for step, current in sorted(steps.items())
        if step - 1 in steps
    ]


def fork_overlap(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Compare only immutable prompt content on the common prefix, at equal steps.
    groups: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in data:
        if r["event"] == "layer_pages":
            groups[
                (
                    r["run_id"],
                    r["rank"],
                    r["layer_id"],
                    r["step"],
                    r["host"],
                    r["page_size_tokens"],
                    r["hot_cache_tokens"],
                    r["replacement_policy"],
                )
            ][r["rid"]] = r
    output = []
    for key, requests in groups.items():
        ordered = sorted(requests)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                left, right = requests[a], requests[b]
                common = set()
                for x, y in zip(left["page_hashes"], right["page_hashes"]):
                    if x != y:
                        break
                    common.add(x)
                if not common:
                    continue
                ap = {p["content_page"] for p in left["pages"]} & common
                bp = {p["content_page"] for p in right["pages"]} & common
                output.append(
                    dict(
                        run_id=key[0],
                        rank=key[1],
                        layer=key[2],
                        step=key[3],
                        host=key[4],
                        page_size_tokens=key[5],
                        hot_cache_tokens=key[6],
                        replacement_policy=key[7],
                        left=a,
                        right=b,
                        common_prefix_pages=len(common),
                        jaccard=ratio(len(ap & bp), len(ap | bp)),
                    )
                )
    return output


def trace_time_ns(microseconds: Any) -> int:
    # Ascend exports epoch timestamps as strings; float loses sub-us precision.
    return round(Decimal(str(microseconds)) * 1000)


def trace_ranges(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    events = raw["traceEvents"] if isinstance(raw, dict) else raw
    lanes = {
        (e.get("pid"), e.get("tid")): e["args"]["name"]
        for e in events
        if e.get("ph") == "M" and e.get("name") == "thread_name"
    }
    result = []
    for e in events:
        if e.get("ph") != "X":
            continue
        lane = lanes.get((e.get("pid"), e.get("tid")), "")
        semantic = e.get("name", "").startswith("mempool:")
        match = re.search(r"Stream\s*(\d+)", lane, re.I)
        if match is None:
            continue
        args = e.get("args", {})
        device_task = (
            str(args.get("Physic Stream Id")) == match.group(1) and "Task Id" in args
        )
        if semantic and "mstx" not in lane.lower() and not device_task:
            continue  # host MSTX durations are not device durations
        if not semantic and re.search(r"hccl|hcom", e.get("name", ""), re.I) is None:
            continue
        result.append(
            dict(
                name=e["name"][8:] if semantic else "hccl_task:" + e["name"],
                start_ns=trace_time_ns(e["ts"]),
                end_ns=trace_time_ns(e["ts"]) + trace_time_ns(e["dur"]),
                stream=int(match.group(1)),
                task_id=args.get("Task Id"),
                trace=str(path),
            )
        )
    return recover_ranges(path, result)


def sweep(
    spans: list[dict[str, Any]], start: int, end: int
) -> Iterator[tuple[int, int, list[dict[str, Any]]]]:
    """Partition at interval boundaries; only overlapping spans are inspected."""
    changes: dict[int, list[tuple[bool, int]]] = defaultdict(list)
    for i, span in enumerate(spans):
        left, right = max(start, span["start_ns"]), min(end, span["end_ns"])
        if left < right:
            changes[left].append((True, i))
            changes[right].append((False, i))
    points = sorted({start, end} | changes.keys())
    active = {}
    for a, b in zip(points, points[1:]):
        for added, i in changes[a]:
            if added:
                active[i] = spans[i]
            else:
                active.pop(i, None)
        yield a, b, list(active.values())


def device_steps(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in ranges:
        by_trace[span["trace"]].append(span)
    output = []
    for trace, trace_spans in by_trace.items():
        forwards = sorted(
            (r for r in trace_spans if r["name"].startswith("forward:")),
            key=lambda r: r["start_ns"],
        )
        starts = [r["start_ns"] for r in forwards]
        nested: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for span in trace_spans:
            if span["name"].startswith("forward:"):
                continue
            i = bisect_right(starts, span["start_ns"]) - 1
            if i >= 0 and span["start_ns"] < span["end_ns"] <= forwards[i]["end_ns"]:
                nested[i].append(span)
        for i, forward in enumerate(forwards):
            start, end = forward["start_ns"], forward["end_ns"]
            spans = nested[i]
            durations: dict[str, int] = defaultdict(int)
            busy: dict[str, int] = defaultdict(int)
            sources: dict[str, int] = defaultdict(int)
            intervals = []
            for span in spans:
                busy[span["name"].split(":")[0]] += span["end_ns"] - span["start_ns"]
            for a, b, active in sweep(spans, start, end):
                main = [
                    s
                    for s in active
                    if s.get("logical_stream", s["stream"])
                    == forward.get("logical_stream", forward["stream"])
                    and not s["name"].startswith("layer:")
                ]
                selected = min(
                    main, key=lambda s: s["end_ns"] - s["start_ns"], default=None
                )
                kind = selected["name"].split(":")[0] if selected else "other_device"
                if kind == "materialize_wait" and selected is not None:
                    layer = selected["name"].split(":")[-1]
                    same_layer = [
                        s for s in active if s["name"].split(":")[-1] == layer
                    ]
                    source = next(
                        (
                            s["name"].split(":")[0][5:]
                            for s in same_layer
                            if s["name"].startswith(("miss_P_remote:", "miss_D_local:"))
                        ),
                        None,
                    )
                    if any(s["name"].startswith("observer_io:") for s in same_layer):
                        kind = "observer_exposed"
                    elif source is not None:
                        kind = "miss_exposed"
                        sources[source] += b - a
                    else:
                        kind = "cache_manage"  # resolve/current supply/event overhead
                kind = {
                    "collective": "collective_exposed",
                    "hccl_task": "collective_exposed",
                    "cache_wait": "cache_manage",
                    "observer": "observer_exposed",
                    "write_through": "write_exposed",
                }.get(kind, kind)
                durations[kind] += b - a
                intervals.append(dict(start_ns=a, end_ns=b, kind=kind))
            materialize = {}
            for ready in (
                s for s in spans if s["name"].startswith("materialize_ready:")
            ):
                layer = ready["name"].split(":")[-1]
                endings = [
                    s["end_ns"]
                    for s in spans
                    if s["name"]
                    in (
                        f"materialize:{layer}",
                        f"miss_io:{layer}",
                        f"cache_hit:{layer}",
                    )
                    and s["start_ns"] >= ready["end_ns"]
                ]
                if endings:
                    materialize[layer] = max(endings) - ready["end_ns"]
            # Baseline/phase1 have the original cache_manage boundary ending
            # immediately before copy_ready; use the same branch envelope.
            for layer in {
                s["name"].split(":")[-1]
                for s in spans
                if s["name"].startswith("miss_io:")
            }:
                branches = [
                    s
                    for s in spans
                    if s["name"] in (f"miss_io:{layer}", f"cache_hit:{layer}")
                ]
                ready = [
                    s["end_ns"]
                    for s in spans
                    if s["name"] == f"cache_manage:{layer}"
                    and s["end_ns"] <= min(b["start_ns"] for b in branches)
                ]
                if layer not in materialize and ready:
                    materialize[layer] = max(b["end_ns"] for b in branches) - max(ready)
            fused = any(s["name"].startswith("materialize:") for s in spans)
            output.append(
                dict(
                    materialize_ns_by_layer=materialize,
                    miss_exposed_upper_ns_by_layer=materialize,
                    source_time_ns=None if fused else dict(sources),
                    source_time_reason="fused" if fused else "split",
                    forward_ids=forward["name"][8:],
                    trace=trace,
                    device_forward_ns=end - start,
                    segments_ns=dict(durations),
                    busy_ns=dict(busy),
                    intervals=intervals,
                    phase_names=sorted(
                        {
                            s["name"]
                            for s in spans
                            if not s["name"].startswith("hccl_task:")
                        }
                    ),
                    miss_exposed_by_source_ns=None if fused else dict(sources),
                    hccl_task_count=sum(
                        s["name"].startswith("hccl_task:") for s in spans
                    ),
                    coverage="one rank, disjoint main-stream intervals; side-stream busy time is separate",
                )
            )
    return output


def step_timing(
    data: list[dict[str, Any]], traces: list[Path], device: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Align through profiler host MSTX anchors, with the measured anchor uncertainty."""
    anchors = {}
    for path in traces:
        raw = json.loads(path.read_text())
        events = raw["traceEvents"] if isinstance(raw, dict) else raw
        for e in events:
            name = e.get("name", "")
            if name.startswith("mempool:clock:") and e.get("ph") in ("X", "i", "I"):
                _, _, ids, before = name.split(":")
                anchors[str(path), ids, int(before)] = trace_time_ns(e["ts"])
    anchors_by_token = {
        (ids, before): (trace, ts) for (trace, ids, before), ts in anchors.items()
    }
    host_anchors = {}
    results_by_key = {}
    controls = {}
    collectors: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in data:
        scope = (r["host"], r["role"], r["pid"])
        if r["event"] == "clock_anchor":
            token = (r["forward_ids"], r["before_ns"])
            if token in anchors_by_token:
                trace, ts = anchors_by_token[token]
                host_anchors[trace, r["forward_ids"]] = (r, ts)
        elif r["event"] == "token_ready":
            results_by_key[scope, r["forward_id"]] = r
        elif r["event"] == "control_tick":
            controls[scope, r["tick_id"]] = r
        elif r["event"] == "observer_collect":
            collectors[scope].append(r)
    output = []
    for step in device:
        ids = step["forward_ids"]
        pair = host_anchors.get((step["trace"], ids))
        if pair is None:
            continue
        anchor, anchor_ts = pair
        scope = (anchor["host"], anchor["role"], anchor["pid"])
        results = [
            results_by_key[scope, int(fid)]
            for fid in ids.split("-")
            if (scope, int(fid)) in results_by_key
        ]
        if not results:
            continue
        begin = min(r["tick_ns"] for r in results)
        end = max(r["ready_ns"] for r in results)
        offset = anchor_ts - (anchor["before_ns"] + anchor["after_ns"]) // 2
        spans = [
            dict(
                start_ns=r["start_ns"] - offset,
                end_ns=r["end_ns"] - offset,
                kind=r["kind"],
                priority=2,
            )
            for r in step["intervals"]
        ]
        ticks = {r["tick_id"] for r in results}
        spans.extend(
            dict(
                start_ns=r["begin_ns"],
                end_ns=r["end_ns"],
                kind="control_wait",
                priority=1,
            )
            for tick in ticks
            if (scope, tick) in controls
            for r in [controls[scope, tick]]
        )
        spans.extend(
            dict(
                start_ns=r["mono_ns"] - r["elapsed_ns"],
                end_ns=r["mono_ns"],
                kind="observer_exposed",
                priority=1,
            )
            for r in collectors[scope]
        )
        spans = [
            dict(s, start_ns=max(begin, s["start_ns"]), end_ns=min(end, s["end_ns"]))
            for s in spans
            if s["end_ns"] > begin and s["start_ns"] < end
        ]
        partition: dict[str, int] = defaultdict(int)
        for a, b, active in sweep(spans, begin, end):
            chosen = max(active, key=lambda s: s["priority"], default=None)
            partition[chosen["kind"] if chosen else "residual"] += b - a
        output.append(
            dict(
                host=anchor["host"],
                role=anchor["role"],
                rank=anchor["rank"],
                run_id=anchor["run_id"],
                forward_ids=ids,
                rids=[r["rid"] for r in results],
                t_step_ns=end - begin,
                segments_ns=dict(partition),
                partition_error_ns=end - begin - sum(partition.values()),
                anchor_uncertainty_ns=anchor["after_ns"] - anchor["before_ns"],
                coverage=ratio(end - begin - partition["residual"], end - begin),
                residual="host submission/queueing, sampling, output handling and unmarked work; not attributed to IO or collectives",
                clock_domain=anchor["clock_domain"],
                trace=step["trace"],
            )
        )
    return output


def operation_timing(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in data:
        if "op_id" in r:
            groups[
                (r["run_id"], r["host"], r["role"], r["rank"], r.get("rid"), r["op_id"])
            ].append(r)
    output = []
    for key, events in groups.items():
        stamps = {r["event"]: r["mono_ns"] for r in events}
        row = dict(
            run_id=key[0],
            host=key[1],
            role=key[2],
            rank=key[3],
            rid=key[4],
            op_id=key[5],
            timestamps_ns=stamps,
        )
        for field, left, right in (
            ("queue_ns", "executor_enqueue", "executor_start"),
            ("library_ns", "library_submit_begin", "library_complete"),
            ("poll_delay_ns", "library_complete", "copy_poll_observed"),
            ("ack_delay_ns", "copy_complete_observed", "ack_sent"),
        ):
            row[field] = (
                stamps[right] - stamps[left]
                if left in stamps and right in stamps
                else None
            )
        row["zero_work"] = any(r.get("zero_work", False) for r in events)
        row["receipts"] = [r for r in events if r["event"] == "copy_complete_observed"]
        row["plans"] = [r for r in events if r["event"] == "copy_plan"]
        output.append(row)
    return output


def lifecycle(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in data:
        groups[(r["run_id"], r.get("rid"), r["host"], r["role"], r["rank"])].append(r)
    output = []
    for key, events in groups.items():
        if not key[1]:
            continue
        row = dict(run_id=key[0], rid=key[1], host=key[2], role=key[3], rank=key[4])
        phases = []

        def phase(name: str, left: Any, right: Any) -> None:
            phases.append(
                dict(
                    phase=name,
                    begin_ns=left,
                    end_ns=right,
                    elapsed_ns=right - left
                    if left is not None and right is not None
                    else None,
                    clock_domain=events[0]["clock_domain"],
                )
            )

        if key[3] == "main":
            request = next(
                (r["received_ns"] for r in events if r["event"] == "request_received"),
                None,
            )
            first = next(
                (
                    r["mono_ns"]
                    for r in events
                    if r["event"] == "observation_first_token"
                ),
                None,
            )
            finished = next(
                (
                    r["mono_ns"]
                    for r in events
                    if r["event"] == "control_received" and r["kind"] == "finished"
                ),
                None,
            )
            frees = [
                r["mono_ns"]
                for r in events
                if r["event"] == "control_received" and r["kind"] == "free_ack"
            ]
            row.update(
                ttft_main_observed_ns=first - request
                if first is not None and request is not None
                else None,
                t_tail_ns=max(frees) - finished
                if len(frees) == 2 and finished is not None
                else None,
            )
            route = next(
                (r["mono_ns"] for r in events if r["event"] == "route_reserved"), None
            )
            phase("route_and_ledger_reservation", request, route)
            for command in (
                r
                for r in events
                if r["event"] == "command_sent"
                and r["kind"] in ("alloc", "load_back", "dispatch")
            ):
                ack = next(
                    (
                        r["mono_ns"]
                        for r in events
                        if r["event"] == "control_received"
                        and r["op_id"] == command["op_id"]
                        and r["kind"] == command["kind"] + "_ack"
                    ),
                    None,
                )
                phase(
                    command["side"] + "_" + command["kind"] + "_round_trip",
                    command["mono_ns"],
                    ack,
                )
            for side in ("prefill", "decode"):
                sent = next(
                    (
                        r["mono_ns"]
                        for r in events
                        if r["event"] == "command_sent"
                        and r["kind"] == "free"
                        and r["side"] == side
                    ),
                    None,
                )
                ack = next(
                    (
                        r["mono_ns"]
                        for r in events
                        if r["event"] == "control_received"
                        and r["kind"] == "free_ack"
                        and r["side"] == side
                    ),
                    None,
                )
                row["t_free_" + side + "_ns"] = (
                    ack - sent if ack is not None and sent is not None else None
                )
        else:
            row["boundaries"] = [
                r
                for r in events
                if r["event"]
                in (
                    "send_complete_observed",
                    "handoff_arrived_observed",
                    "three_conditions_ready",
                    "request_end",
                    "stop_boundary",
                    "first_token_ready",
                )
            ]
            row["steps"] = [r for r in events if r["event"] == "token_ready"]
            row["operations"] = [r for r in events if "op_id" in r]
            stamps = {r["event"]: r["mono_ns"] for r in events}
            if key[3] == "prefill":
                begins = [
                    r["tick_ns"]
                    for r in events
                    if r["event"] == "step_start" and r["mode"] == "prefill"
                ]
                phase(
                    "prefill_to_handoff_send_complete",
                    min(begins) if begins else None,
                    stamps.get("send_complete_observed"),
                )
            else:
                phase(
                    "handoff_arrival_to_three_conditions_ready",
                    stamps.get("handoff_arrived_observed"),
                    stamps.get("three_conditions_ready"),
                )
                phase(
                    "three_conditions_ready_to_first_output",
                    stamps.get("three_conditions_ready"),
                    stamps.get("first_token_ready"),
                )
            for submit in (
                r
                for r in events
                if r["event"] == "copy_submit" and r["kind"] == "load_back"
            ):
                done = next(
                    (
                        r["mono_ns"]
                        for r in events
                        if r["event"] == "copy_complete_observed"
                        and r["op_id"] == submit["op_id"]
                    ),
                    None,
                )
                phase("load_back_local", submit["mono_ns"], done)
        row["ttft_phases_local"] = phases
        row["phase_accounting"] = (
            "local-clock causal intervals; overlapping phases and different hosts are not summed"
        )
        output.append(row)
    return output


def summarize(
    data: list[dict[str, Any]], traces: list[Path], *, check_ranks: bool = True
) -> dict[str, Any]:
    runs = [r for r in data if r["event"] == "run"]
    trace_enabled = any(r.get("trace_enabled", True) for r in runs)
    if not trace_enabled:
        traces = []
    ranges = [r for path in traces for r in trace_ranges(path)]
    sharing = [
        r
        for r in data
        if r["event"] == "layer_pages" and not r["ordered_selection_equal"]
    ]
    device = device_steps(ranges)
    timing = step_timing(data, traces, device)
    operations = operation_timing(data)
    runs = [r for r in data if r["event"] == "run"]
    workers = {(r["run_id"], r["host"], r["role"], r["rank"]): r for r in runs}
    pages = {
        (r["run_id"], r["host"], r["role"], r["rank"], r["forward_id"], r["layer_id"])
        for r in data
        if r["event"] == "layer_pages"
    }
    expected: set[tuple[Any, ...]] = set()
    for r in data:
        if r["event"] == "forward_submitted" and r["mode"] == "decode":
            config = workers[r["run_id"], r["host"], r["role"], r["rank"]]
            if r["step"] % config["sample_every"] == 0:
                expected.update(
                    (
                        r["run_id"],
                        r["host"],
                        r["role"],
                        r["rank"],
                        r["forward_id"],
                        layer,
                    )
                    for layer in range(config["layers"])
                )
    byte_checks = []
    for op in operations:
        for receipt in op["receipts"]:
            plans = op["plans"]
            if plans:
                planned = sum(b["nbytes"] for p in plans for b in p["blocks"])
                physical = receipt["main_bytes"] + receipt["index_bytes"]
                byte_checks.append(
                    dict(
                        rid=op["rid"],
                        op_id=op["op_id"],
                        rank=op["rank"],
                        planned=planned,
                        receipt=physical,
                        passed=planned == physical,
                    )
                )
    timed = {
        (r["run_id"], r["host"], r["role"], r["rank"], int(fid))
        for r in timing
        for fid in r["forward_ids"].split("-")
    }
    outputs = {
        (r["run_id"], r["host"], r["role"], r["rank"], r["forward_id"])
        for r in data
        if r["event"] == "token_ready"
    }
    required = ("indexer", "attention", "moe", "collective")
    missing = []
    if not workers:
        missing.append("worker run manifests")
    signatures: dict[tuple[Any, ...], set[tuple[Any, ...]]] = defaultdict(set)
    for r in data:
        if r["event"] == "forward_submitted":
            signatures[r["run_id"], r["host"], r["role"], r["rank"]].add(
                (r["rid"], r["forward_id"], r["mode"])
            )
    for run_id, host, role in {(r["run_id"], r["host"], r["role"]) for r in runs}:
        local = [
            r
            for r in runs
            if (r["run_id"], r["host"], r["role"]) == (run_id, host, role)
        ]
        if check_ranks and {r["rank"] for r in local} != set(
            range(local[0]["tp_size"])
        ):
            missing.append(f"{host}/{role}: missing expected TP rank manifests")
        rank_sets = [signatures[run_id, host, role, r["rank"]] for r in local]
        if (
            not rank_sets
            or not rank_sets[0]
            or any(s != rank_sets[0] for s in rank_sets)
        ):
            missing.append(f"{host}/{role}: rank forward coverage differs or is empty")
    if expected - pages:
        missing.append(
            f"{len(expected - pages)} expected layer/request/step page records"
        )
    if trace_enabled and outputs - timed:
        missing.append(
            f"{len(outputs - timed)} output steps lack profiler clock/device attribution"
        )
    if trace_enabled and (
        not device
        or any(not any(s["busy_ns"].get(k, 0) for s in device) for k in required)
    ):
        missing.append("indexer/attention/MoE/collective device ranges")
    if device and not any(s["hccl_task_count"] for s in device):
        missing.append("HCCL device task evidence")
    timing_by_key = {(r["trace"], r["forward_ids"]): r for r in timing}
    phase_gaps = []
    for s in device:
        row = timing_by_key.get((s["trace"], s["forward_ids"]))
        if row is None:
            continue
        config = workers[row["run_id"], row["host"], row["role"], row["rank"]]
        names = set(s["phase_names"])
        for layer in range(config["layers"]):
            for phase in (
                ("attention", "moe", "write_through")
                + (("cache_manage", "miss_io") if row["role"] == "decode" else ())
                + (("indexer",) if layer in config["producer_layers"] else ())
            ):
                if not any(
                    name == f"{phase}:{layer}"
                    or (
                        name.startswith(phase + ":")
                        and re.search(rf"layers\.{layer}(?:\.|$)", name)
                    )
                    for name in names
                ):
                    phase_gaps.append(
                        dict(
                            rank=row["rank"],
                            forward_ids=s["forward_ids"],
                            layer=layer,
                            phase=phase,
                        )
                    )
        if not s["hccl_task_count"] or not s["busy_ns"].get("collective"):
            phase_gaps.append(
                dict(
                    rank=row["rank"],
                    forward_ids=s["forward_ids"],
                    phase="collective device task/range",
                )
            )
    if phase_gaps:
        missing.append(f"{len(phase_gaps)} forward/layer device phase gaps")
    if sharing:
        missing.append("producer/shared-layer ordered token selection differs")
    if any(not r["passed"] for r in byte_checks):
        missing.append("physical copy plan/receipt byte reconciliation")
    if any(op["plans"] and not op["receipts"] for op in operations):
        missing.append("copy plan lacks completion receipt")
    read_checks = [r for r in data if r["event"] == "read_byte_reconciliation"]
    if any(not r["passed"] for r in read_checks):
        missing.append("selected-page/source-mask read byte reconciliation")
    observed_reads = {
        (r["run_id"], r["host"], r["role"], r["rank"], r["forward_id"])
        for r in read_checks
    }
    if {k[:5] for k in expected} - observed_reads:
        missing.append("sampled decode forward lacks read byte reconciliation")
    for s in device:
        s.pop("intervals")
        s.pop("phase_names")
    return dict(
        schema_version=1,
        status="passed" if not missing else "incomplete",
        missing=missing,
        coverage=dict(
            workers=len(workers),
            page_records=len(pages),
            expected_page_records=len(expected),
            output_steps=len(outputs),
            timed_steps=len(timed),
            device_timing="checked" if trace_enabled else "not-collected",
            external_pending=[
                "user k/2k/4k cache-capacity logs; this run uses the actual configured cache capacity"
            ],
            not_applicable=["replica reads", "remote writes"],
        ),
        miss_rate=miss_rate(data),
        concentration=concentration(data),
        cross_step=cross_step(data),
        fork_overlap=fork_overlap(data),
        selection_mismatches=len(sharing),
        phase_gaps=phase_gaps,
        lifecycle=lifecycle(data),
        operations=operations,
        byte_checks=byte_checks,
        read_byte_checks=read_checks,
        operation_device_ranges=[
            r for r in ranges if r["name"].startswith(("load_back:", "fill:"))
        ],
        device_steps=device,
        step_timing=timing,
    )


def summarize_directory(root: Path) -> dict[str, Any]:
    """Analyze one worker at a time; large page logs stay out of the peer channel."""
    processes = []
    missing: list[str] = []
    main_rids: set[str] = set()
    critical_steps: dict[tuple[str, str], dict[str, Any]] = {}
    copy_bytes: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: dict(main_bytes=0, index_bytes=0)
    )
    signatures: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file in sorted(root.glob("*.jsonl")):
        data = records([file])
        identity = data[0]
        role, rank = identity["role"], identity["rank"]
        report: dict[str, Any]
        if role == "main":
            requests = lifecycle(data)
            main_rids.update(r["rid"] for r in requests)
            main_missing = [
                f"{r['rid']}: Main request/first-token/finished/free boundary missing"
                for r in requests
                if any(
                    r.get(k) is None
                    for k in (
                        "ttft_main_observed_ns",
                        "t_tail_ns",
                        "t_free_prefill_ns",
                        "t_free_decode_ns",
                    )
                )
            ]
            if not requests:
                main_missing.append("Main request lifecycles missing")
            report = dict(
                schema_version=1,
                status="incomplete" if main_missing else "passed",
                missing=main_missing,
                lifecycle=requests,
            )
        else:
            report = summarize(
                data,
                list((root / f"trace-{role}-{rank}").rglob("trace_view.json")),
                check_ranks=False,
            )
            run = next(r for r in data if r["event"] == "run")
            signature = sorted(
                (r["rid"], r["forward_id"], r["mode"])
                for r in data
                if r["event"] == "forward_submitted"
            )
            signatures[role].append(
                dict(
                    rank=rank,
                    tp_size=run["tp_size"],
                    rids=sorted({r[0] for r in signature}),
                    signature=hashlib.sha256(
                        json.dumps(signature).encode()
                    ).hexdigest(),
                )
            )
        path = root / (file.stem + "-summary.json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        write_table(path.with_suffix(".md"), report)
        for step in report.get("step_timing", []):
            key = role, step["forward_ids"]
            if (
                key not in critical_steps
                or step["t_step_ns"] > critical_steps[key]["t_step_ns"]
            ):
                critical_steps[key] = step
        for operation in report.get("operations", []):
            for receipt in operation["receipts"]:
                key = receipt["rid"], receipt["kind"]
                for field in ("main_bytes", "index_bytes"):
                    copy_bytes[key][field] += receipt.get(field, 0)
        processes.append(
            dict(
                role=role,
                rank=rank,
                status=report["status"],
                report=str(path),
                missing=report["missing"],
            )
        )
        missing.extend(f"{role}/{rank}: {item}" for item in report["missing"])
        del data, report
    for role, workers in signatures.items():
        if {r["rank"] for r in workers} != set(range(workers[0]["tp_size"])):
            missing.append(f"{role}: expected TP rank logs missing")
        if len({r["signature"] for r in workers}) != 1:
            missing.append(f"{role}: rank forward coverage differs")
    if not signatures:
        missing.append("worker observation files missing")
    if "prefill" in signatures and not any(p["role"] == "main" for p in processes):
        missing.append("Main observation file missing on prefill side")
    if "prefill" in signatures and main_rids != set(signatures["prefill"][0]["rids"]):
        missing.append("Main/worker request lifecycle coverage differs")
    return dict(
        schema_version=1,
        status="incomplete" if missing else "passed",
        missing=missing,
        coverage=dict(
            processes=processes,
            analysis="one worker at a time; per-process reports retain all four offline outputs",
        ),
        critical_steps=list(critical_steps.values()),
        critical_rank_rule="largest complete T_step duration per batch; all categories from that same rank",
        copy_bytes=[dict(rid=k[0], kind=k[1], **v) for k, v in copy_bytes.items()],
    )


def compare_copy_bytes(
    actual: list[dict[str, Any]], functional: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    copied = {(r["rid"], r["kind"]): r for r in actual}
    checks = []
    for entry in functional:
        for kind in ("load_back", "fill"):
            if kind not in entry.get("prefix", {}):
                continue
            expected = entry["prefix"][kind]
            observed = copied.get((entry["rid"], kind), {})
            checks.append(
                dict(
                    rid=entry["rid"],
                    kind=kind,
                    expected=expected,
                    actual=observed,
                    passed=all(
                        observed.get(field) == expected[field]
                        for field in ("main_bytes", "index_bytes")
                    ),
                )
            )
    return checks


def write_table(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "| Layer | Cache tokens | n | Cache miss | IO miss |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in result.get("miss_rate", []):
        lines.append(
            f"| {r['layer']} | {r['hot_cache_tokens']} | {r['n']} | {r['token_cache_miss_rate']} | {r['io_miss_rate']} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "miss-rate",
            "concentration",
            "cross-step",
            "fork-overlap",
            "summarize",
        ),
    )
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--trace", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result: Any
    if (
        args.command == "summarize"
        and len(args.inputs) == 1
        and args.inputs[0].is_dir()
        and not args.trace
    ):
        result = summarize_directory(args.inputs[0])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if result["status"] == "passed" else 1
    data = records(args.inputs)
    functions = {
        "miss-rate": miss_rate,
        "concentration": concentration,
        "cross-step": cross_step,
        "fork-overlap": fork_overlap,
    }
    result = (
        summarize(data, args.trace)
        if args.command == "summarize"
        else functions[args.command](data)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.command == "summarize":
        write_table(args.output.with_suffix(".md"), result)
    return 1 if args.command == "summarize" and result["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
