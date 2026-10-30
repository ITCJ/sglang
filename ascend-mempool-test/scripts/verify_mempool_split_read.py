"""Verify archived K1-03 evidence from instrumented revision 0337b9b9ad.

The live runner no longer emits these temporary probes after user acceptance.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

import mempool_logical_cases
import verify_mempool_session as session

ASSIGNMENTS = ("round_robin", "contiguous")


def check_split_read(
    log: Path, snapshots: list[dict[str, Any]], assignment: str
) -> dict[str, Any]:
    acks = session.log_records(log, "split_read_ack")
    buffers = session.log_records(log, "split_read_buffers")
    if len(buffers) != 16 or {b["rank"] for b in buffers} != set(range(16)):
        raise ValueError("missing or duplicate first-restore buffer evidence")
    hits = [s["completed"][-1] for s in snapshots if s["completed"][-1]["hit_len"]]
    if [d["hit_len"] for d in hits] != [4096, 3968] or len(acks) != 2:
        raise ValueError("split_read requires complete B/C completion evidence")
    requests = []
    # Actual fixed GLM-5.2 BF16 layout: 78 MLA layers and 21 Index K layers.
    token_bytes = 2 * (78 * 576 + 21 * 128)
    for done in hits:
        operation = done["reads"]["load_back"]
        rows = operation["ack"]["ranks"]
        if [r["rank"] for r in rows] != list(range(16)):
            raise ValueError("split_read lacks a complete ordered rank group")
        acknowledgements = [
            a
            for a in acks
            if (a["rid"], a["op_id"]) == (done["rid"], operation["command"]["op_id"])
        ]
        if len(acknowledgements) != 1:
            raise ValueError("missing or duplicate split_read ack")
        count = done["hit_len"] // 128
        width = (count + 15) // 16
        groups = []
        for reader in range(16):
            if assignment == "round_robin":
                groups.append(list(range(reader, count, 16)))
            else:
                base, extra = divmod(count, 16)
                start = reader * base + min(reader, extra)
                groups.append(list(range(start, start + base + (reader < extra))))
        slots = [p for group in groups for p in group + [None] * (width - len(group))]
        expected_bytes = dict(
            main=16 * width * 128 * 1152, index=16 * width * 128 * 256
        )
        traffic = 0
        for rank, row in enumerate(rows):
            proof = row["split_read"]
            expected = groups[rank]
            if (
                proof["assignment"] != assignment
                or proof["readers"] != 16
                or proof["logical_pages"] != expected
                or proof["padded_pages"] != (count + 15) // 16
                or proof["read_bytes"] != len(expected) * 128 * token_bytes
                or proof["oracle_read_bytes"] != 0
                or proof["oracle"] != "computed_source"
                or proof["gathered_verified"] is not True
                or proof["native_verified"] is not True
                or proof["path"] != ["H2D", "all_gather", "scatter"]
                or proof["launches"] != dict(H2D=99, all_gather=99, scatter=99)
                or proof["data_buffers"] != 2
                or proof["buffer_bytes"] != expected_bytes
                or proof["slot_table_builds"] != 1
                or proof["slot_table_rows"] != done["hit_len"]
                or proof["slot_pages"] != slots
                or not 0 < proof["completed_ns"] < acknowledgements[0]["ack_ns"]
            ):
                raise ValueError(
                    "split_read traffic/oracle/completion evidence differs"
                )
            traffic += proof["read_bytes"]
            if done is hits[0]:
                record = next(b for b in buffers if b["rank"] == rank)
                if (
                    record["rid"] != done["rid"]
                    or record["slots"] != 16 * width
                    or record["page_size"] != 128
                    or record["data_buffers"] != 2
                    or record["buffer_bytes"] != expected_bytes
                ):
                    raise ValueError(
                        "first-restore buffer sizes differ from the receipt"
                    )
        if traffic != done["hit_len"] * token_bytes:
            raise ValueError("split_read total bytes differ from the hit")
        requests.append(
            dict(rid=done["rid"], hit_len=done["hit_len"], read_bytes=traffic)
        )
    return dict(
        status="passed",
        assignment=assignment,
        requests=requests,
        oracle="gathered slots and native HBM rows exactly equal independent computed-source snapshots",
        bm_readback=False,
        completion="ack follows every rank's device-complete receipt",
        traffic="split_read data copies only; no extra BM reads for acceptance",
    )


def summarize(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = session.summarize(root)
    reports, responses = [], []
    for stage, assignment in zip(session.SERVICE_STAGES, ASSIGNMENTS):
        directory = root / stage
        registrations = json.loads((directory / "registrations.json").read_text())
        for role in ("prefill", "decode"):
            if any(
                r["prefix_read_assignment"] != assignment
                for r in registrations[role]["ranks"]
            ):
                raise ValueError(
                    "split_read registration differs from the tested assignment"
                )
        reports.append(
            check_split_read(
                directory / "prefill.log",
                json.loads((directory / "snapshots.json").read_text()),
                assignment,
            )
        )
        reports[-1]["timings"] = timing_rows(
            json.loads((directory / "snapshots.json").read_text())
        )
        responses.append(json.loads((directory / "responses.json").read_text()))
    comparisons = compare_strategies(responses[0], responses[1])
    result.update(
        coverage="K1-03 split_read functional acceptance (two strategies)",
        split_read=reports,
        strategy_comparison=comparisons,
    )
    write_timing_table(root, reports)
    return result


def timing_rows(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for snapshot in snapshots:
        done = snapshot["completed"][-1]
        if not done["hit_len"]:
            continue
        operation = done["reads"]["load_back"]
        timings = [r["split_read"]["timings_ms"] for r in operation["ack"]["ranks"]]
        phases = ("H2D", "all_gather", "scatter")
        if len(timings) != 16 or any(
            set(t) != {*phases, "T_lb_rank"}
            or any(not math.isfinite(v) or v < 0 for v in t.values())
            for t in timings
        ):
            raise ValueError("incomplete device timing evidence")
        # Use one rank for all three device components; never add unrelated rank maxima.
        slowest = max(range(16), key=lambda rank: sum(timings[rank][p] for p in phases))
        device = {p: timings[slowest][p] for p in phases}
        op2 = operation["elapsed_ms"]
        control = op2 - sum(device.values())
        if not math.isfinite(op2) or control < 0:
            raise ValueError(
                "op2 wall interval is smaller than its measured device path"
            )
        rows.append(
            dict(
                rid=done["rid"],
                hit_len=done["hit_len"],
                rank=slowest,
                op2=op2,
                control=control,
                **device,
                T_lb_rank=[t["T_lb_rank"] for t in timings],
            )
        )
    return rows


def write_timing_table(root: Path, reports: list[dict[str, Any]]) -> None:
    note = (
        "诊断计时，无性能门槛。三项设备分段取同一rank（其分段合计最大），"
        "控制=Main的op2墙钟减三项设备分段，含排队、建表、CPU验证与回执等待。"
        "V2开启，不能将控制余量解释为纯生产控制耗时。"
    )
    baseline = (
        Path(__file__).resolve().parents[2]
        / ".scratch/mempool-baseline/verification/baseline01-002/metrics.csv"
    )
    with baseline.open() as stream:
        historical = [
            r
            for r in csv.DictReader(stream)
            if r["metric"]
            in ("T_op2_load_back", "T_lb_rank_min", "T_lb_rank_max", "T_lb_rank_p50")
            and r["case"] in ("B", "C")
        ]
    lines = [
        "# K1-03 op2分段",
        "",
        note,
        "",
        "历史基线baseline01-002（134b22d90d）仅作量级参照；验证开关和样本数量不同。旧基线没有设备三段计时，不能补造数据。",
        "",
        "| 策略 | 命中 | 指标 | n | p50 ms | min ms | max ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in historical:
        lines.append(
            f"| baseline01-002 | {row['case']} | {row['metric']} | {row['n']} | {row['p50']} | {row['min']} | {row['max']} |"
        )
    for report in reports:
        for row in report["timings"]:
            for name in ("T_lb_rank", "op2", "H2D", "all_gather", "scatter", "control"):
                values = row[name] if name == "T_lb_rank" else [row[name]]
                lines.append(
                    f"| {report['assignment']} | {row['hit_len']} | {name} | {len(values)} | {statistics.median(values):.3f} | {min(values):.3f} | {max(values):.3f} |"
                )
    (root / "split-read-timings.md").write_text("\n".join(lines) + "\n")
    (root / "split-read-timings.json").write_text(
        json.dumps(dict(note=note, baseline=historical, reports=reports), indent=2)
        + "\n"
    )


def compare_strategies(
    first_run: dict[str, Any], second_run: dict[str, Any]
) -> list[dict[str, Any]]:
    if any(
        len(run["cases"]) != 6 or len(run["results"]) != 6
        for run in (first_run, second_run)
    ):
        raise ValueError("reader strategies require all six request outputs")
    if first_run["cases"] != second_run["cases"]:
        raise ValueError("reader strategies were tested with different inputs")
    comparisons = [
        mempool_logical_cases.compare_answers(case, first, second)
        for case, first, second in zip(
            first_run["cases"], first_run["results"], second_run["results"]
        )
    ]
    if any(c["status"] != "passed" for c in comparisons):
        raise ValueError("reader assignment changed the required logical answers")
    return comparisons
