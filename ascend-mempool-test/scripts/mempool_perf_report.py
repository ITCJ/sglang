"""Offline, fail-closed stage-0 perf report from archived per-process events."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from mempool_perf_outputs import (
    OUTPUT_POLICY,
    STRICT_POLICY,
    compare_outputs,
    output_analysis,
)

CASES = ("A", "B", "C", "E")
MAIN = (
    "T_main_e2e",
    "T_route",
    "T_op2_load_back",
    "T_op1_fill",
    "T_ready",
    "T_gen",
    "T_op3_admit",
    "T_tail",
    "T_free_P",
    "T_free_D",
)
P_METRICS = ("T_lb_rank", "T_prefill", "T_wt_rank", "T_send", "T_hbm_release")
D_METRICS = (
    "T_gate",
    "T_prompt_ready",
    "T_index_ready",
    "T_handoff_ready",
    "T_first",
    "T_step_first",
    "T_step_p50",
    "T_step_p90",
    "T_step_p99",
    "T_step_max",
    "T_finish",
    "backend_e2e_latency",
)
METRICS = (
    *MAIN,
    *(f"{metric}_{side}" for metric in P_METRICS for side in ("min", "max")),
    *D_METRICS,
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return dict(n=0, p50=None, min=None, max=None, p90=None, p99=None)
    return dict(
        n=len(values),
        p50=quantile(values, 0.5),
        min=min(values),
        max=max(values),
        p90=quantile(values, 0.9),
        p99=quantile(values, 0.99),
    )


def check_sample(
    case: str,
    fixture: dict,
    reference: dict,
    result: dict,
    snapshot: dict,
    previous: dict,
    *,
    strict_ids: bool = True,
) -> str:
    """V1/V3/V6 only; no tensor readbacks or copy-adapter probes."""
    if not all(
        isinstance(value, dict) for value in (reference, result, snapshot, previous)
    ):
        raise ValueError("missing/malformed response or state snapshot")
    ids = result.get("output_ids")
    for label, response in (("output", result), ("reference", reference)):
        output, meta = response.get("output_ids"), response.get("meta_info", {})
        if (
            not isinstance(output, list)
            or len(output) != 128
            or any(type(i) is not int for i in output)
            or meta.get("completion_tokens") != 128
            or meta.get("prompt_tokens") != len(fixture["input_ids"])
            or meta.get("finish_reason", {}).get("type") != "length"
        ):
            raise ValueError(
                label
                + " must contain exactly 128 IDs, correct prompt length and finish by length"
            )
    if strict_ids and ids != reference.get("output_ids"):
        first = next(
            (
                i
                for i, pair in enumerate(zip(ids, reference.get("output_ids", [])))
                if pair[0] != pair[1]
            ),
            None,
        )
        raise ValueError(f"V3 complete output IDs differ from reference; first={first}")
    if snapshot.get("fault") or snapshot.get("active") is not None:
        raise ValueError("request has not drained/returned its resources")
    done = snapshot["completed"][-1]
    if (
        done["result"] != result
        or done["hit_len"] != dict(A=0, B=4096, C=3968, E=0)[case]
    ):
        raise ValueError("V1 hit length or completed request identity differs")
    if len(fixture["input_ids"]) != dict(A=4096, B=5120, C=4096, E=4096)[case]:
        raise ValueError("fixture length differs")
    expected = dict(A=32, B=40, C=40, E=40)[case]
    if set(snapshot["ledger"]) != {"p_alive", "d_alive", "d_hbm", "d_rows", "p_prefix"}:
        raise ValueError("V6 incomplete ledger schema")
    for name, quota in snapshot["ledger"].items():
        if set(quota) != {"total", "free", "outstanding"} or any(
            type(n) is not int or n < 0 for n in quota.values()
        ):
            raise ValueError("V6 invalid ledger capacity")
        if quota["total"] != quota["free"] + quota["outstanding"] or quota[
            "outstanding"
        ] != (expected if name == "p_prefix" else 0):
            raise ValueError("V6 ledger equation/ownership failed: " + name)
    if snapshot["ledger"]["p_prefix"] != dict(
        total=40, free=40 - expected, outstanding=expected
    ):
        raise ValueError("V6 prefix occupancy differs")
    directory = snapshot["directory"]
    if len(directory) != expected:
        raise ValueError("V6 directory size differs from prefix ledger")
    for key, node in directory.items():
        prefix = node["prefix"]
        if (
            node["lock_ref"]
            or node["source_ops"]
            or node["owner"] is not None
            or prefix["state"] != "ready"
            or prefix["lock_ops"]
        ):
            raise ValueError("V6 retained prefix is locked/not published")
        if key in previous and previous[key]["prefix"] != prefix:
            raise ValueError("V6 retained prefix changed")
    if not previous.keys() <= directory.keys():
        raise ValueError("V6 retained prefix disappeared")
    return str(done["rid"])


def assess_sample(
    case: str,
    fixture: dict,
    reference: dict,
    result: dict,
    snapshot: dict,
    previous: dict,
) -> dict:
    """Record independent logical/state checks; an ID mismatch is diagnostic only."""
    diagnostic = compare_outputs(fixture, reference, result)
    errors = list(diagnostic["errors"])
    rid = ""
    try:
        rid = check_sample(
            case, fixture, reference, result, snapshot, previous, strict_ids=False
        )
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return dict(rid=rid, valid=not errors, errors=errors, output_diagnostics=diagnostic)


def read_events(root: Path) -> list[dict]:
    events = []
    for name in ("router.log", "prefill.log", "decode.log"):
        path = root / name
        if not path.exists():
            continue
        for line_no, line in enumerate(
            path.read_text(errors="replace").splitlines(), 1
        ):
            marker = "mempool_perf data="
            if marker in line:
                value = json.loads(line.split(marker, 1)[1])
                if value.get("schema") != 1 or type(value.get("mono_ns")) is not int:
                    raise ValueError(f"invalid perf event: {path}:{line_no}")
                value["source"] = f"{name}:{line_no}"
                events.append(value)
    (root / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    )
    groups: dict[tuple, list] = defaultdict(list)
    for event in events:
        groups[(event["process"], event["rank"], event["pid"])].append(event)
    for (process, rank, pid), items in groups.items():
        (root / f"events-{process}-rank{rank}-pid{pid}.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in items)
        )
    return events


def clock_of(event: dict) -> tuple:
    return event["host"], event["pid"], event["process"], event["rank"]


def interval(start: dict, end: dict) -> float:
    if clock_of(start) != clock_of(end):
        raise ValueError("refusing to subtract different process clocks")
    delta = (end["mono_ns"] - start["mono_ns"]) / 1e6
    if delta < 0:
        raise ValueError(
            "negative event interval: " + start["event"] + " -> " + end["event"]
        )
    return delta


def request_samples(
    events: list[dict],
    record: dict,
    round_number: int,
    samples: list[dict] | None = None,
) -> list[dict]:
    selected = [event for event in events if event["rid"] == record["rid"]]
    if samples is None:
        samples = []

    def one(event: str, process: str = "main", rank: int = 0, **filters: Any) -> dict:
        found = [
            e
            for e in selected
            if e["event"] == event
            and e["process"] == process
            and e["rank"] == rank
            and all(e.get(k) == v for k, v in filters.items())
        ]
        if len(found) != 1:
            raise ValueError(
                f"expected one {process}/{rank}/{event}/{filters}, got {len(found)}"
            )
        return found[0]

    def value(metric: str, duration: float, clock: Any, **extra: Any) -> None:
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("invalid duration: " + metric)
        samples.append(
            dict(
                round=round_number,
                case=record["case"],
                rid=record["rid"],
                metric=metric,
                value_ms=duration,
                clock=clock,
                valid=True,
                warmup=round_number == 1,
                **extra,
            )
        )

    def duration(metric: str, start: dict, end: dict, **extra: Any) -> float:
        delta = interval(start, end)
        value(metric, delta, clock_of(start), **extra)
        return delta

    req, dispatch = one("request"), one("dispatch_send", role="decode")
    ready, finished = one("ready_tokens"), one("finished")
    free = [one("free_ack", role=role) for role in ("prefill", "decode")]
    last_free = max(free, key=lambda e: e["mono_ns"])
    duration("T_main_e2e", req, one("response_sent"))
    duration("T_route", req, dispatch)
    duration("T_ready", dispatch, ready)
    duration("T_gen", ready, finished)
    duration("T_op3_admit", finished, one("publish"))
    duration("T_tail", finished, last_free)
    for role, suffix in (("prefill", "P"), ("decode", "D")):
        start, end = one("free_send", role=role), one("free_ack", role=role)
        if start["op_id"] != end["op_id"]:
            raise ValueError("free ack operation mismatch")
        duration("T_free_" + suffix, start, end)
    if record["case"] in ("B", "C"):
        for op, metric in (("load_back", "T_op2_load_back"), ("fill", "T_op1_fill")):
            start, end = one(op + "_send"), one(op + "_ack")
            if start["op_id"] != end["op_id"]:
                raise ValueError("prefix ack operation mismatch")
            duration(metric, start, end)
    rank_values: dict[str, list[tuple[float, tuple]]] = defaultdict(list)
    for rank in range(16):

        def p(name: str, **filters: Any) -> dict:
            return one(name, "prefill", rank, **filters)

        begin, end = p("forward_begin", step=1), p("forward_end", step=1)
        pairs = [
            ("T_prefill", begin, end),
            ("T_wt_rank", begin, p("write_complete", step=1)),
            ("T_send", p("send_begin"), p("handoff_complete")),
            ("T_hbm_release", end, p("hbm_release")),
        ]
        if record["case"] in ("B", "C"):
            pairs.append(("T_lb_rank", p("load_back_begin"), p("load_back_end")))
        for metric, start, finish in pairs:
            delta = duration(metric, start, finish, rank=rank)
            rank_values[metric].append((delta, clock_of(start)))
    for metric, ranks in rank_values.items():
        for name, choose in (("min", min), ("max", max)):
            delta, clock = choose(ranks, key=lambda item: item[0])
            value(metric + "_" + name, delta, clock, rank_reduction=name, rank_count=16)

    def d(event: str, **filters: Any) -> dict:
        return one(event, "decode", 0, **filters)

    dispatch_d = d("dispatch_received")
    for name in ("prompt_ready", "index_ready", "handoff_ready"):
        interval(d(name), d("gate_ready"))
    for metric, name in (
        ("T_gate", "gate_ready"),
        ("T_prompt_ready", "prompt_ready"),
        ("T_index_ready", "index_ready"),
        ("T_handoff_ready", "handoff_ready"),
        ("T_first", "first_token_sent"),
    ):
        duration(metric, dispatch_d, d(name))
    begins = [
        e
        for e in selected
        if e["process"] == "decode" and e["rank"] == 0 and e["event"] == "forward_begin"
    ]
    steps = sorted(e["step"] for e in begins)
    ends = [
        e
        for e in selected
        if e["process"] == "decode" and e["rank"] == 0 and e["event"] == "forward_end"
    ]
    if (
        not steps
        or steps != list(range(1, len(steps) + 1))
        or sorted(e["step"] for e in ends) != steps
    ):
        raise ValueError("missing, duplicate or unfinished decode steps")
    values = [
        duration("T_step", d("forward_begin", step=i), d("forward_end", step=i), step=i)
        for i in steps
    ]
    value("T_step_first", values[0], clock_of(dispatch_d))
    summary = stats(values)
    for name in ("p50", "p90", "p99", "max"):
        value(
            "T_step_" + name,
            summary[name],
            clock_of(dispatch_d),
            actual_steps=len(steps),
            expected_steps=127,
        )
    duration("T_finish", d("forward_end", step=steps[-1]), d("finished_send"))
    backend = record["result"]["meta_info"].get("e2e_latency")
    if type(backend) not in (int, float):
        raise ValueError("missing backend e2e_latency")
    value(
        "backend_e2e_latency",
        backend * 1000,
        "decode backend meta_info.e2e_latency; own clock",
    )
    record["actual_steps"] = len(steps)
    record["first_emission_tokens"] = d("first_token_sent")["output_tokens"]
    record["gate_arrivals_ns"] = {
        name: d(name)["mono_ns"]
        for name in ("prompt_ready", "index_ready", "handoff_ready", "gate_ready")
    }
    record["gate_clock"] = clock_of(dispatch_d)
    queued = [
        e for e in selected if e["process"] == "main" and e["event"] == "op_queued"
    ]
    spans = []
    for item in queued:
        sent = one(item["kind"] + "_send", role=item["role"], op_id=item["op_id"])
        duration(
            "T_command_queue", item, sent, op_id=item["op_id"], operation=item["kind"]
        )
        spans.append((item["mono_ns"], sent["mono_ns"]))
    if spans:
        total, last = 0, -1
        for start, end in sorted(spans):
            total += max(0, end - max(start, last))
            last = max(last, end)
        value("T_main_queue_wait", total / 1e6, clock_of(req))
    return samples


def applicable(metric: str, case: str) -> bool:
    return not (
        case in ("A", "E")
        and (
            metric.startswith("T_lb_rank")
            or metric in ("T_op1_fill", "T_op2_load_back")
        )
    )


def build_report(root: Path, explanations: dict[str, str] | None = None) -> dict:
    explanations = explanations or {}
    if not isinstance(explanations, dict) or any(
        not isinstance(reason, str) or not reason.strip()
        for reason in explanations.values()
    ):
        raise ValueError("stability explanations must be nonempty evidence strings")
    samples, records, failures = [], [], []
    cases = read_json(root / "cases.json")
    references = read_json(root / "reference/responses.json")["results"]
    manifest = read_json(root / "perf-manifest.json")
    policy = manifest.get("output_policy", STRICT_POLICY)
    if policy not in (OUTPUT_POLICY, STRICT_POLICY):
        raise ValueError(f"unknown output policy: {policy}")
    diagnostic_outputs = policy == OUTPUT_POLICY
    a, b, c, e = [item["input_ids"] for item in cases]
    if (
        list(map(len, (a, b, c, e))) != [4096, 5120, 4096, 4096]
        or b[:4096] != a
        or c != a
        or e[:128] == a[:128]
    ):
        raise ValueError("archived fixtures do not preserve ABCE token-prefix contract")
    if (
        len(references) != 4
        or type(manifest["rounds"]) is not int
        or manifest["rounds"] < 5
    ):
        raise ValueError("four references and at least five rounds are required")
    for fixture in cases:
        sampling = fixture["sampling_params"]
        if (
            sampling.get("temperature") != 0
            or sampling.get("top_k", 1) != 1
            or sampling.get("top_p", 1) != 1
            or sampling.get("ignore_eos") is not True
            or sampling.get("max_new_tokens") != 128
            or sampling.get("stop")
            or sampling.get("stop_regex")
            or "min_new_tokens" in sampling
        ):
            raise ValueError("archived sampling differs from the fixed perf contract")
    for filename in (
        "identity.json",
        "effective-config.json",
        "topology.json",
        "decode-topology.json",
    ):
        try:
            value = read_json(root / filename)
            if not isinstance(value, dict) or not value:
                raise ValueError("empty or malformed snapshot")
        except (OSError, ValueError) as exc:
            failures.append(f"required snapshot {filename}: {exc}")
    for round_number in range(1, manifest["rounds"] + 1):
        folder = root / f"round-{round_number:02d}"
        try:
            outcome, events = read_json(folder / "outcome.json"), read_events(folder)
        except (OSError, ValueError) as exc:
            failures.append(f"round {round_number}: {exc}")
            records.extend(
                dict(
                    round=round_number,
                    case=case,
                    rid="",
                    valid=False,
                    warmup=round_number == 1,
                    error=str(exc),
                )
                for case in CASES
            )
            continue
        previous, invalid = {}, ""
        registrations = outcome.get("initial", {}).get("registrations", {})
        if set(registrations) != {"prefill", "decode"} or any(
            [r.get("rank") for r in registrations.get(role, {}).get("ranks", [])]
            != list(range(16))
            or any(
                "numa_node" not in r or "numa_placement_verified" not in r
                for r in registrations.get(role, {}).get("ranks", [])
            )
            for role in ("prefill", "decode")
        ):
            invalid = "missing complete TP16 rank-to-NUMA registrations"
        if (
            outcome.get("initial", {}).get("directory") != {}
            or outcome.get("initial", {})
            .get("ledger", {})
            .get("p_prefix", {})
            .get("outstanding")
            != 0
        ):
            invalid = "round did not start with an empty prefix"
        if not outcome.get("paired_cleanup"):
            invalid = "paired process cleanup missing"
        for index, case in enumerate(CASES):
            record = dict(
                round=round_number,
                case=case,
                rid="",
                valid=False,
                warmup=round_number == 1,
            )
            rows = []
            errors = []
            try:
                saved = outcome["samples"][index]
                if saved.get("case") != case:
                    raise ValueError("round case order differs from ABCE")
                record.update(
                    {
                        k: v
                        for k, v in saved.items()
                        if k
                        in ("rid", "result", "snapshot", "error", "execution_error")
                    }
                )
                if diagnostic_outputs and "result" in record:
                    assessed = assess_sample(
                        case,
                        cases[index],
                        references[index],
                        record["result"],
                        record.get("snapshot", {}),
                        previous,
                    )
                    record["output_diagnostics"] = assessed["output_diagnostics"]
                    errors.extend(assessed["errors"])
                    if assessed["rid"] and assessed["rid"] != record["rid"]:
                        errors.append("timing RID differs from completed request RID")
                if record.get("rid") and "result" in record:
                    request_samples(events, record, round_number, rows)
                if invalid:
                    raise ValueError("earlier round failure: " + invalid)
                if diagnostic_outputs:
                    if saved.get("execution_error"):
                        errors.append(saved["execution_error"])
                    if "result" not in record:
                        errors.append(saved.get("error", "request not run/no response"))
                    if errors:
                        raise ValueError("; ".join(errors))
                    record.pop("error", None)
                    record["valid"] = True
                    continue
                if saved.get("error"):
                    raise ValueError(saved["error"])
                expected_rid = check_sample(
                    case,
                    cases[index],
                    references[index],
                    saved["result"],
                    saved["snapshot"],
                    previous,
                )
                if expected_rid != record["rid"]:
                    raise ValueError("timing RID differs from completed request RID")
                previous = saved["snapshot"]["directory"]
                record["valid"] = True
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                if str(exc) not in errors and str(exc) != "; ".join(errors):
                    errors.append(str(exc))
                error = "; ".join(errors)
                if not diagnostic_outputs:
                    invalid = error
                record.update(valid=False, error=error)
                for sample in rows:
                    sample.update(valid=False, reason=error)
                rows.append(
                    dict(
                        round=round_number,
                        case=case,
                        rid=record["rid"],
                        metric="invalid_sample",
                        value_ms=None,
                        clock=None,
                        valid=False,
                        warmup=round_number == 1,
                        reason=error,
                    )
                )
            finally:
                if diagnostic_outputs and isinstance(record.get("snapshot"), dict):
                    previous = record["snapshot"].get("directory", previous)
                records.append(record)
                samples.extend(rows)
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sample in samples:
        if sample["valid"] and not sample["warmup"]:
            grouped[sample["metric"], sample["case"]].append(sample["value_ms"])
    table, unstable = [], []
    for metric in (*METRICS, "T_step"):
        for case in CASES:
            if not applicable(metric, case):
                table.append(dict(metric=metric, case=case, applicable=False))
                continue
            row = dict(
                metric=metric,
                case=case,
                applicable=True,
                **stats(grouped[metric, case]),
            )
            row["request_n"] = sum(
                r["valid"] and not r["warmup"] and r["case"] == case for r in records
            )
            if row["request_n"] < 4 or (metric != "T_step" and row["n"] < 4):
                failures.append(
                    f"{metric}/{case}: fewer than four valid measured requests"
                )
            # T_step aggregates intra-request variation. Stability is evaluated
            # across requests, including their per-request step summaries.
            if metric != "T_step" and row["n"] >= 4:
                ratio = (
                    row["max"] / row["min"]
                    if row["min"]
                    else (1 if row["max"] == 0 else math.inf)
                )
                if ratio > 1.5:
                    key = f"{metric}/{case}"
                    unstable.append(
                        dict(
                            key=key,
                            ratio=ratio if math.isfinite(ratio) else "inf",
                            explanation=explanations.get(key),
                        )
                    )
            table.append(row)
    if any(not row["explanation"] for row in unstable):
        failures.append(
            "max/min > 1.5 needs a recorded explanation (or additional rounds and re-evaluation)"
        )
    invalid_count = sum(not r["valid"] for r in records)
    if diagnostic_outputs and invalid_count:
        failures.append(
            f"{invalid_count} requests failed recorded checks (including warmup)"
        )
    outputs = (
        output_analysis(cases, references, records) if diagnostic_outputs else None
    )
    if outputs is not None:
        write_json(root / "output-analysis.json", outputs)
    result = dict(
        schema=2 if diagnostic_outputs else 1,
        status="failed" if failures else "passed",
        user_accepted=False,
        output_policy=policy,
        execution_complete=all("result" in r for r in records),
        output_stability=outputs["stability"] if outputs is not None else [],
        invalid_samples=invalid_count,
        warmup_samples=sum(r["warmup"] for r in records),
        failures=failures,
        unstable=unstable,
        table=table,
        requests=[
            {k: v for k, v in r.items() if k not in ("snapshot", "result")}
            for r in records
        ],
    )
    (root / "samples.jsonl").write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples)
    )
    write_json(root / "perf-report.json", result)
    render_markdown(root, result, samples, manifest)
    return result


def render_markdown(
    root: Path, report: dict, samples: list[dict], manifest: dict
) -> None:
    lines = [
        "# 阶段 0 单请求基线（Ascend NPU）",
        "",
        f"自动检查：**{report['status']}**；用户 NPU 验收：待用户决定（user_accepted=false）。",
        f"ABCE 全轮响应收集完整：{report['execution_complete']}；输出策略：{report['output_policy']}。",
        f"有效统计排除第1轮预热；无效样本 {report['invalid_samples']}。重启整套服务清空 prefix。",
        "",
        "## 版本 / 配置 / 拓扑",
        "",
        "```json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    for name in (
        "identity.json",
        "effective-config.json",
        "topology.json",
        "decode-topology.json",
    ):
        path = root / name
        lines.extend(
            [
                f"### {name}",
                "",
                "```json",
                json.dumps(read_json(path), ensure_ascii=False, indent=2)
                if path.exists()
                else "MISSING",
                "```",
                "",
            ]
        )
    first = root / "round-01/outcome.json"
    if first.exists():
        registrations = read_json(first).get("initial", {}).get("registrations", {})
        lines.extend(
            ["| 主机角色 | rank | 注册 NUMA | 物理落点已验证 |", "|---|---:|---:|---|"]
        )
        for role, registration in registrations.items():
            for rank in registration["ranks"]:
                lines.append(
                    f"| {role} | {rank['rank']} | {rank['numa_node']} | {rank.get('numa_placement_verified', False)} |"
                )
        controls = {
            role: registration["ranks"][0].get("perf_controls", {})
            for role, registration in registrations.items()
            if registration.get("ranks")
        }
        lines.extend(
            [
                "",
                "实际worker运行控制（各端rank0；完整注册在outcome.json）：",
                "",
                "```json",
                json.dumps(controls, ensure_ascii=False, indent=2),
                "```",
            ]
        )
    if report["output_policy"] == OUTPUT_POLICY:
        outputs = read_json(root / "output-analysis.json")
        lines.extend(
            [
                "",
                "## 输出与逻辑答案",
                "",
                "完整128-token序列不一致仅作记录，不导致跳过C/E或停止轮次。逻辑答案按开头第一个JSON对象与独立标准答案逐字段、逐类型比较；不搜索后文补救错误答案。续写原文保留，不对其逻辑正确性作保证。",
                "",
                "参考与实际完整text/IDs、解析答案及续写见 `output-analysis.json`；原始响应仍保存在reference/responses.json和各轮outcome.json。首差使用从0开始的索引。",
                "",
                "| round | case | 参考逻辑 | 实际逻辑 | 完整IDs一致 | 首差索引 | 样本检查 |",
                "|---:|---|---|---|---|---:|---|",
            ]
        )
        for record in report["requests"]:
            diagnostic = record.get("output_diagnostics", {})
            difference = diagnostic.get("ids", {})
            lines.append(
                f"| {record['round']} | {record['case']} | "
                f"{diagnostic.get('reference', {}).get('status', 'not-run')} | "
                f"{diagnostic.get('actual', {}).get('status', 'not-run')} | "
                f"{difference.get('equal', 'unknown')} | {difference.get('first_difference')} | "
                f"{'valid' if record['valid'] else 'invalid'} |"
            )
        lines.extend(
            [
                "",
                "### 跨轮输出稳定性（包含预热和无效样本）",
                "",
                "相同ID序列归为一组；每轮与首个实际输出的首差、逻辑答案分组均保留。稳定性差异本身只作诊断。参考A/C同输入比较单独保留。",
                "",
                "```json",
                json.dumps(
                    dict(
                        reference_A_vs_C=outputs["reference_A_vs_C"],
                        stability=outputs["stability"],
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
            ]
        )
    lines.extend(
        [
            "",
            "## 指标主表（ms）",
            "",
            "P 的 min/max 是每请求先取16 rank极值，再跨轮统计；T_step 为逐步池化分布，其余为每请求一条。",
            "",
            "| metric | case | n | requests | p50 | min | max | p90 | p99 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    def number(value: Any) -> str:
        return "—" if value is None else f"{value:.3f}"

    for row in report["table"]:
        if not row["applicable"]:
            lines.append(f"| {row['metric']} | {row['case']} | N/A | | | | | | |")
        else:
            values = [
                row[k] if k not in ("p90", "p99") or row["metric"] == "T_step" else None
                for k in ("p50", "min", "max", "p90", "p99")
            ]
            lines.append(
                f"| {row['metric']} | {row['case']} | {row['n']} | {row['request_n']} | "
                + " | ".join(map(number, values))
                + " |"
            )
    for case in ("A", "B"):
        lines.extend(
            [
                "",
                f"## {case} 的 e2e 拆分（ms）",
                "",
                "| round | e2e | route | ready | gen | tail | residual |",
                "|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for round_number in range(2, manifest["rounds"] + 1):
            values = {
                s["metric"]: s["value_ms"]
                for s in samples
                if s["valid"] and s["case"] == case and s["round"] == round_number
            }
            if "T_main_e2e" in values:
                parts = [
                    values[k]
                    for k in ("T_main_e2e", "T_route", "T_ready", "T_gen", "T_tail")
                ]
                residual = parts[0] - sum(parts[1:])
                lines.append(
                    f"| {round_number} | "
                    + " | ".join(number(v) for v in (*parts, residual))
                    + " |"
                )
        lines.append(
            "\n残差 = Main 最后一个 free_ack 到完整 HTTP 响应 write_eof；包含后端完整响应未到齐的等待、Main处理/JSON序列化及HTTP发送。其余消息往返已包含在相应阶段。T_op1/2和T_op3与主拆分重叠，不能再次相加。"
        )
    lines.extend(
        [
            "",
            "## 固定等待与观察误差",
            "",
            "- `mempool_router_transport.MainServer.pump` 每轮 sleep(0.01)，收发需等待下一次轮询。每个跨轮唤醒有0–10ms量化等待；没有删除或缩短它。",
            "- Main依次等待D alloc、P alloc、D dispatch、P dispatch ack；命中另有load_back/fill ack。T_route/T_ready包含这些串行往返。",
            "- `mempool_subrouter.MempoolSubrouter.advance` 每个scheduler tick有两次TP gather；每rank设备完成需等待tick观察及全rank共识。",
            "- `ticket10_processes.Peer.expect` 的100ms等待、启动就绪轮询、轮间重启均在请求计时外。",
            "- 完成时刻为现有tick观察到设备event完成的主机单调时刻；与*_submitted一起归档。查询延迟包含在T_prefill/T_wt/T_step，无法从主机事件推导纯kernel耗时。",
            "",
        ]
    )
    for case in CASES:
        e2e = next(
            r
            for r in report["table"]
            if r["metric"] == "T_main_e2e" and r["case"] == case
        )
        if e2e.get("p50"):
            waits = [
                s["value_ms"]
                for s in samples
                if s["valid"]
                and not s["warmup"]
                and s["case"] == case
                and s["metric"] == "T_main_queue_wait"
            ]
            if waits:
                wait = quantile(waits, 0.5)
                lines.append(
                    f"{case}：实测Main命令入队→发送区间并集p50={wait:.3f}ms，占e2e p50的{100 * wait / e2e['p50']:.2f}%。包含待pump唤醒与Main处理；不含远端完成到收包的不可观察等待。"
                )
            lines.append(
                f"{case}：单次10ms轮询满周期相当于e2e p50的{1000 / e2e['p50']:.2f}%。没有从命令数推断总等待上界；多个等待可能与模型计算重叠。"
            )
    lines.extend(
        [
            "",
            "## 稳定性 / 无效样本",
            "",
            "```json",
            json.dumps(
                dict(
                    unstable=report["unstable"],
                    failures=report["failures"],
                    requests=report["requests"],
                ),
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "## 已知混淆项",
            "",
            "- 历史11.12秒来自128输入/48输出；本票4096/5120输入、128输出的分段不能数值分解那次旧请求。",
            "- 非流式：T_first是首次真实IPC发送，报告保存当次输出token数；不能称客户端TTFT/ITL。",
            "- D Graph补齐BS16，真实请求1；P eager、单chunk、串行。重启使每轮都有启动/Graph预热，但首轮ABCE仍排除统计。",
            "- 阶段0不支持显式rank_numa_map，现有8 NUMA连续均分只有一套5轮。K1将改变NUMA使用与共享；注册分配不证明物理页落点。",
            "- Ascend环境以驱动/CANN/torch-npu/HCCL/MemFabric及NPU互联为准，CUDA/nvidia-smi/GPUDirect不适用。RDMA/设备直传是否启用须看归档证据，未观察到就标未知。",
            "- 计时日志与非阻塞event查询有成本；各版本必须原样使用同入口。T_wt与forward可重叠，不是可相加的分段。",
            "- 输出采用core-answer-v1：核心答案与标准答案核对，强制128-token续写差异仅记录；逻辑或结构检查失败仍标无效，计时值保留。"
            if report["output_policy"] == OUTPUT_POLICY
            else "- 此归档采用旧版严格128-token完整IDs策略；离线重算保持历史判定，不追认用户验收。",
            "",
            "原始数据：`samples.jsonl`；每轮`events*.jsonl`、三个进程日志、`outcome.json`。离线重跑不需NPU。",
            "",
        ]
    )
    (root / "baseline.md").write_text("\n".join(lines))


def generate_report(root: Path, explanations: dict[str, str] | None = None) -> dict:
    """Early startup/reference failures still produce an explicit failed report."""
    try:
        return build_report(root, explanations)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        result = dict(
            schema=1,
            status="failed",
            user_accepted=False,
            invalid_samples=None,
            failures=[f"{type(exc).__name__}: {exc}"],
            table=[],
            requests=[],
        )
        write_json(root / "perf-report.json", result)
        write_json(
            root / "output-analysis.json",
            dict(
                schema=1,
                user_accepted=False,
                status="incomplete",
                failures=result["failures"],
                note="inspect reference/responses.json and round-*/outcome.json for raw outputs",
            ),
        )
        if not (root / "samples.jsonl").exists():
            (root / "samples.jsonl").write_text("")
        (root / "baseline.md").write_text(
            "# 阶段 0 单请求基线（Ascend NPU）\n\n状态：**failed**；未形成有效基线。\n\n"
            + f"归档不完整或报告校验失败：{type(exc).__name__}: {exc}\n\n"
            + "参考/启动/轮次中断的原始日志保留在本目录；详情见summary.json。\n"
            + "真实NPU验收未完成，不能据此关闭本票。\n"
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="archived prefill directory")
    parser.add_argument(
        "--stability-explanations",
        type=Path,
        help='JSON mapping "metric/case" to evidence-backed explanation',
    )
    args = parser.parse_args(argv)
    explanations = (
        read_json(args.stability_explanations) if args.stability_explanations else {}
    )
    report = generate_report(args.root, explanations)
    print(
        json.dumps(
            dict(
                status=report["status"],
                invalid_samples=report["invalid_samples"],
                report=str(args.root / "baseline.md"),
            )
        )
    )
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
