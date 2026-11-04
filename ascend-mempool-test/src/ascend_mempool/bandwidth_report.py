"""Aggregate completion-time samples without summing overlapping reader clocks."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cases[row["case"]].append(row)
    result = {}
    for name, records in cases.items():
        by_sample: dict[int, list[dict[str, Any]]] = defaultdict(list)
        by_rank: dict[str, list[float]] = defaultdict(list)
        rates, overlaps, cores, first_completions = [], [], [], []
        segments: dict[str, list[float]] = defaultdict(list)
        for row in records:
            by_sample[row["sample"]].append(row)
            by_rank[str(row["rank"])].extend(row["durations_ms"])
        for group in by_sample.values():
            if all("first_operation_done_ns" in r for r in group):
                first_completions.append(
                    (
                        max(r["first_operation_done_ns"] for r in group)
                        - min(r["first_operation_start_ns"] for r in group)
                    )
                    / 1e6
                )
            span_ns = max(r["done_ns"] for r in group) - min(
                r["start_ns"] for r in group
            )
            overlap_ns = min(r["done_ns"] for r in group) - max(
                r["start_ns"] for r in group
            )
            if span_ns <= 0:
                raise ValueError("sample has no positive completion interval")
            rates.append(
                sum(r["bytes_per_op"] * len(r["durations_ms"]) for r in group) / span_ns
            )
            overlaps.append(max(0, overlap_ns) / span_ns)
            cores.append(sum(r["cpu_seconds"] for r in group) / (span_ns / 1e9))
            traffic: dict[str, int] = defaultdict(int)
            for r in group:
                for segment, nbytes in r["segments"].items():
                    traffic[segment] += nbytes * len(r["durations_ms"])
            for segment, nbytes in traffic.items():
                segments[segment].append(nbytes / span_ns)
        latencies = [v for values in by_rank.values() for v in values]
        result[name] = dict(
            first_group_completion_median_ms=median(first_completions)
            if first_completions
            else None,
            first_group_completion_samples_ms=first_completions,
            repeats=len(by_sample),
            operation_samples=len(latencies),
            median_GBps=median(rates),
            min_overlap_fraction=min(overlaps),
            p99_ms=percentile(latencies, 0.99),
            median_cpu_cores=median(cores),
            per_rank={
                rank: dict(median_ms=median(v), p99_ms=percentile(v, 0.99), n=len(v))
                for rank, v in by_rank.items()
            },
            segments_GBps={key: median(v) for key, v in segments.items()},
        )
    return result


def save_report(
    root: Path, rows: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, Any]:
    summaries = summarize(rows)
    report = dict(metadata, measurements=summaries, user_accepted=False)
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    with (root / "summary.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "case",
                "median_GBps",
                "p99_ms",
                "operation_samples",
                "cpu_cores",
                "min_overlap",
            ]
        )
        for name, row in summaries.items():
            writer.writerow(
                [
                    name,
                    row["median_GBps"],
                    row["p99_ms"],
                    row["operation_samples"],
                    row["median_cpu_cores"],
                    row["min_overlap_fraction"],
                ]
            )
    return report


def validate_measurements(
    config: Any, rows: list[dict[str, Any]], pipes: list[dict[str, Any]]
) -> None:
    from .bandwidth import build_plan, write_pages

    plan = {case.name: case for case in build_plan(config) if not case.pipeline}
    expected = {
        (case.name, case.side, rank, sample)
        for case in plan.values()
        for rank in range(config.tp_size if case.operation == "write" else case.readers)
        for sample in range(config.samples)
    }
    actual = [(r["case"], r["side"], r["rank"], r["sample"]) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError(
            f"measurement coverage differs: missing={len(expected - set(actual))}, extra={len(set(actual) - expected)}"
        )
    for r in rows:
        if not (
            r["start_ns"]
            <= r["first_operation_start_ns"]
            < r["first_operation_done_ns"]
            <= r["done_ns"]
        ):
            raise ValueError("invalid first group operation completion evidence")
        case = plan[r["case"]]
        if case.operation == "read":
            nbytes = case.read_blocks(config) * case.block_bytes(config)
        else:
            tokens = sum(
                min(config.page_size, case.tokens - p * config.page_size)
                for p in write_pages(config, case, r["rank"])
            )
            nbytes = tokens * config.row_bytes
            if case.group != "locations":
                nbytes *= 2 if case.targets == "dual" else 1
                if case.targets in ("local", "dual"):
                    nbytes += tokens * 256  # layer 0 produces Index K
        if r["bytes_per_op"] != nbytes or sum(r["segments"].values()) != nbytes:
            raise ValueError(f"{case.name}: byte accounting differs from planned work")
        if len(r["durations_ms"]) != config.iterations or any(
            not math.isfinite(v) or v <= 0 for v in r["durations_ms"]
        ):
            raise ValueError(f"{case.name}: missing or invalid operation timings")
    pipe_keys = [(r["rank"], r["sample"]) for r in pipes]
    if len(pipe_keys) != len(set(pipe_keys)) or set(pipe_keys) != {
        (rank, sample)
        for rank in range(config.tp_size)
        for sample in range(config.samples)
    }:
        raise ValueError("pipeline sample coverage differs")
