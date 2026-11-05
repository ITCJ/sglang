"""Export the same materialization fields for baseline, phase1 and phase2."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from analyze_k2_observation import device_steps, records, step_timing, trace_ranges


def extract(root: Path, stage: str) -> list[dict[str, Any]]:
    data = records([root])
    traces = list(root.rglob("trace_view.json"))
    spans = [s for path in traces for s in trace_ranges(path)]
    devices = device_steps(spans)
    timing = step_timing(data, traces, devices)
    materialized = {
        (
            r["run_id"],
            r["host"],
            r["role"],
            r["rid"],
            r["rank"],
            r["forward_id"],
            r["layer_id"],
        ): r
        for r in data
        if r["event"] == "materialization"
    }
    result = []
    for page in (r for r in data if r["event"] == "layer_pages"):
        key = (
            page["run_id"],
            page["host"],
            page["role"],
            page["rid"],
            page["rank"],
            page["forward_id"],
            page["layer_id"],
        )
        new = materialized.get(key)
        sources = dict(
            P_alive=0,
            D_alive=0,
            **{f"prefix_{i}": 0 for i in range(8)},
            HBM_cache=0,
            HBM_current=0,
        )
        for entry in page["pages"]:
            sources["HBM_cache"] += entry["hit_count"]
            sources["HBM_current"] += entry["current_device_supply_count"]
            for source in entry["sources"]:
                sources[
                    {"P_remote": "P_alive", "D_local": "D_alive"}[source["source"]]
                ] += source["rows"]
        if new:
            sources = new["source_rows"]
        row: dict[str, Any] = dict(
            stage=stage,
            run_id=page["run_id"],
            rid=page["rid"],
            rank=page["rank"],
            step=page["step"],
            layer=page["layer_id"],
            page_size_tokens=page["page_size_tokens"],
            hot_cache_tokens=page["hot_cache_tokens"],
            T_step_ns=None,
            materialize_ns=None,
            miss_exposed_upper_ns=None,
            materialize_wait_ns=None,
            current_dram_rows=0,
            refill_dram_bytes=0,
            source_time_ns=None,
            source_time_reason="fused" if new else "split; see device trace",
            source_rows=sources,
            source_bytes={k: v * 1152 for k, v in sources.items()},
        )
        matches = [
            r
            for r in timing
            if r.get("rank") == page["rank"]
            and r.get("host") == page["host"]
            and r.get("run_id") == page["run_id"]
            and str(page["forward_id"]) in str(r.get("forward_ids", "")).split("-")
        ]
        if len(matches) == 1:
            matched = matches[0]
            row["T_step_ns"] = matched.get("t_step_ns")
            device = next(
                (
                    d
                    for d in devices
                    if d["trace"] == matched["trace"]
                    and d["forward_ids"] == matched["forward_ids"]
                ),
                None,
            )
            if device:
                value = device["materialize_ns_by_layer"].get(str(page["layer_id"]))
                row.update(materialize_ns=value, miss_exposed_upper_ns=value)
        result.append(row)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for stage in ("baseline", "phase1", "phase2"):
        parser.add_argument("--" + stage, type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        r
        for stage in ("baseline", "phase1", "phase2")
        for r in extract(getattr(args, stage), stage)
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "materialization.json").write_text(json.dumps(rows, indent=2) + "\n")
    with (args.output / "materialization.csv").open("w") as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(
                {k: json.dumps(v) if isinstance(v, dict) else v for k, v in row.items()}
                for row in rows
            )


if __name__ == "__main__":
    main()
