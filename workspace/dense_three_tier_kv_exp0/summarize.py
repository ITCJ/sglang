#!/usr/bin/env python3
"""Pair L2/L3 experiment-0 measurements and summarize three runs."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


class SummaryError(RuntimeError):
    pass


def load_measurements(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        candidates = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
        for candidate in candidates:
            with candidate.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("record_type") != "measurement":
                        continue
                    if record.get("accepted") is not True:
                        raise SummaryError(
                            f"unaccepted measurement in {candidate}:{line_number}"
                        )
                    record["_source"] = f"{candidate}:{line_number}"
                    records.append(record)
    if not records:
        raise SummaryError("no accepted measurement records found")
    return records


def summarize(records: list[dict[str, Any]], expected_runs: int = 3) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    manifests: dict[str, set[str]] = defaultdict(set)
    for record in records:
        phase = str(record.get("phase", "")).lower()
        if phase not in {"l2", "l3"}:
            continue
        length = str(record["length"])
        run_id = str(record["run_id"])
        prefix_id = str(record["prefix_id"])
        key = (length, run_id, prefix_id)
        if phase in grouped[key]:
            raise SummaryError(
                f"duplicate {phase} measurement for {key}: "
                f"{grouped[key][phase]['_source']} and {record['_source']}"
            )
        grouped[key][phase] = record
        manifests[length].add(str(record["manifest_sha256"]))

    for length, hashes in manifests.items():
        if len(hashes) != 1:
            raise SummaryError(f"{length} used multiple manifests: {sorted(hashes)}")

    paired: list[dict[str, Any]] = []
    for (length, run_id, prefix_id), phases in sorted(grouped.items()):
        missing = {"l2", "l3"} - set(phases)
        if missing:
            raise SummaryError(
                f"missing {sorted(missing)} measurement for "
                f"{(length, run_id, prefix_id)}"
            )
        l2_ms = float(phases["l2"]["ttft_ms"])
        l3_ms = float(phases["l3"]["ttft_ms"])
        paired.append(
            {
                "length": length,
                "run_id": run_id,
                "prefix_id": prefix_id,
                "ttft_l2_ms": l2_ms,
                "ttft_l3_ms": l3_ms,
                "delta_ttft_ms": l3_ms - l2_ms,
            }
        )

    per_run_pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for pair in paired:
        per_run_pairs[(pair["length"], pair["run_id"])].append(pair)

    run_summaries = []
    for (length, run_id), pairs in sorted(per_run_pairs.items()):
        if len(pairs) != 10:
            raise SummaryError(
                f"{length}/{run_id} has {len(pairs)} pairs; expected exactly 10"
            )
        run_summaries.append(
            {
                "length": length,
                "run_id": run_id,
                "num_pairs": len(pairs),
                "median_ttft_l2_ms": statistics.median(
                    pair["ttft_l2_ms"] for pair in pairs
                ),
                "median_ttft_l3_ms": statistics.median(
                    pair["ttft_l3_ms"] for pair in pairs
                ),
                "median_delta_ttft_ms": statistics.median(
                    pair["delta_ttft_ms"] for pair in pairs
                ),
            }
        )

    by_length: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in run_summaries:
        by_length[run["length"]].append(run)

    aggregate = []
    for length, runs in sorted(by_length.items()):
        if len(runs) != expected_runs:
            raise SummaryError(
                f"{length} has {len(runs)} complete runs; expected {expected_runs}"
            )
        l2_values = [float(run["median_ttft_l2_ms"]) for run in runs]
        l3_values = [float(run["median_ttft_l3_ms"]) for run in runs]
        delta_values = [float(run["median_delta_ttft_ms"]) for run in runs]
        aggregate.append(
            {
                "length": length,
                "num_runs": len(runs),
                "median_of_run_median_ttft_l2_ms": statistics.median(l2_values),
                "min_run_median_ttft_l2_ms": min(l2_values),
                "max_run_median_ttft_l2_ms": max(l2_values),
                "median_of_run_median_ttft_l3_ms": statistics.median(l3_values),
                "min_run_median_ttft_l3_ms": min(l3_values),
                "max_run_median_ttft_l3_ms": max(l3_values),
                "median_of_run_median_delta_ttft_ms": statistics.median(
                    delta_values
                ),
                "min_run_median_delta_ttft_ms": min(delta_values),
                "max_run_median_delta_ttft_ms": max(delta_values),
                "all_run_medians_positive": all(
                    value > 0 for value in delta_values
                ),
            }
        )

    return {
        "schema_version": 1,
        "paired_measurements": paired,
        "run_summaries": run_summaries,
        "aggregate": aggregate,
    }


def markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Experiment 0 Summary",
        "",
        "## Per Run",
        "",
        "| Length | Run | Pairs | Median L2 TTFT (ms) | Median L3 TTFT (ms) | Median Delta (ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for run in summary["run_summaries"]:
        lines.append(
            f"| {run['length']} | {run['run_id']} | {run['num_pairs']} | "
            f"{run['median_ttft_l2_ms']:.3f} | "
            f"{run['median_ttft_l3_ms']:.3f} | "
            f"{run['median_delta_ttft_ms']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Across Runs",
            "",
            "| Length | Runs | L2 Median (ms) | L2 Min | L2 Max | L3 Median (ms) | L3 Min | L3 Max | Delta Median (ms) | Delta Min | Delta Max | All Delta Medians Positive |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in summary["aggregate"]:
        lines.append(
            f"| {item['length']} | {item['num_runs']} | "
            f"{item['median_of_run_median_ttft_l2_ms']:.3f} | "
            f"{item['min_run_median_ttft_l2_ms']:.3f} | "
            f"{item['max_run_median_ttft_l2_ms']:.3f} | "
            f"{item['median_of_run_median_ttft_l3_ms']:.3f} | "
            f"{item['min_run_median_ttft_l3_ms']:.3f} | "
            f"{item['max_run_median_ttft_l3_ms']:.3f} | "
            f"{item['median_of_run_median_delta_ttft_ms']:.3f} | "
            f"{item['min_run_median_delta_ttft_ms']:.3f} | "
            f"{item['max_run_median_delta_ttft_ms']:.3f} | "
            f"{str(item['all_run_medians_positive']).lower()} |"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--expected-runs", type=int, default=3)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = summarize(
            load_measurements(args.inputs), expected_runs=args.expected_runs
        )
        rendered = markdown(result)
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.markdown_output:
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, SummaryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
