"""Summarize opt-in ticket03 probes; this is diagnostic, never acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TAG = "[DEBUG-ticket03-reuse] data="
REQUIRED_CHECKS = {
    "input_ids",
    "positions",
    "query_lengths",
    "kv_lengths",
    "prefix_lengths",
    "extend_lengths",
    "restored_length",
    "suffix_length",
    "block_table",
    "unique_indices",
}


def summarize(path: Path, *, ranks: int = 16) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    parse_errors = 0
    if path.exists():
        with path.open(errors="replace") as log:
            for line in log:
                if TAG not in line:
                    continue
                try:
                    records.append(json.loads(line.split(TAG, 1)[1]))
                except ValueError:
                    parse_errors += 1
    summaries = []
    for rank in range(ranks):
        local = [r for r in records if r["rank"] == rank]
        forwards = [r for r in local if r["stage"] == "forward"]
        comparisons = [r for r in local if r["stage"] == "comparison"]
        baselines = [r for r in local if r["stage"] == "baseline"]
        comparison = comparisons[0] if len(comparisons) == 1 else None
        layers = [
            r
            for r in local
            if r["stage"] == "layer" and comparison and r["rid"] == comparison["rid"]
        ]
        main = sorted(
            (r for r in layers if r["region"] == "main"), key=lambda r: r["layer"]
        )
        index = sorted(
            (r for r in layers if r["region"] == "index"), key=lambda r: r["layer"]
        )
        failed_checks = {}
        checks_complete = True
        for r in forwards:
            checks = r.get("checks", {})
            missing = sorted(
                key for key in REQUIRED_CHECKS if type(checks.get(key)) is not bool
            )
            checks_complete = checks_complete and not missing
            failed = sorted(key for key, passed in checks.items() if passed is not True)
            if missing or failed:
                failed_checks[r["rid"]] = ["missing:" + key for key in missing] + failed
        forward_identity = (
            len(forwards) == 3
            and len({r["rid"] for r in forwards}) == 3
            and [r["hit_len"] for r in forwards] == [0, 4096, 3968]
        )
        identity_ok = (
            forward_identity
            and len(baselines) == 1
            and baselines[0].get("rid") == forwards[0]["rid"]
            and baselines[0].get("page") == 31
            and baselines[0].get("layers") == 99
            and isinstance(forwards[0].get("input_sha256"), str)
            and len(forwards[0]["input_sha256"]) == 64
            and forwards[0]["input_sha256"] == forwards[2].get("input_sha256")
            and comparison is not None
            and comparison.get("rid") == forwards[2]["rid"]
            and comparison.get("reference_rid") == forwards[0]["rid"]
            and comparison.get("hit_len") == 3968
            and comparison.get("main_layers") == 78
            and comparison.get("index_layers") == 21
            and all(r.get("reference_rid") == forwards[0]["rid"] for r in layers)
        )
        complete = (
            identity_ok
            and checks_complete
            and comparison is not None
            and comparison["page"] == 31
            and [r["layer"] for r in main] == list(range(78))
            and [r["layer"] for r in index] == list(range(21))
        )
        summaries.append(
            dict(
                rank=rank,
                complete=complete,
                identity_ok=identity_ok,
                metadata_ok=forward_identity and checks_complete and not failed_checks,
                failed_checks=failed_checks,
                comparison=comparison,
                first_main=next(
                    (r for r in main if r["changed"] or r["nonfinite"]), None
                ),
                first_index=next(
                    (r for r in index if r["changed"] or r["nonfinite"]), None
                ),
                changed_main_layers=sum(r["changed"] > 0 for r in main),
                changed_index_layers=sum(r["changed"] > 0 for r in index),
                nonfinite=sum(r["nonfinite"] for r in layers),
            )
        )
    return dict(
        purpose="diagnostic only; strict output acceptance is unchanged",
        complete=not parse_errors and all(r["complete"] for r in summaries),
        parse_errors=parse_errors,
        ranks=summaries,
    )


def print_summary(report: dict[str, Any]) -> None:
    print(
        "REUSE_DIAGNOSTIC "
        + json.dumps(
            dict(complete=report["complete"], parse_errors=report["parse_errors"])
        ),
        flush=True,
    )
    for row in report["ranks"]:
        compact = {
            key: row[key]
            for key in (
                "rank",
                "complete",
                "identity_ok",
                "metadata_ok",
                "failed_checks",
                "changed_main_layers",
                "changed_index_layers",
                "nonfinite",
            )
        }
        for key in ("first_main", "first_index"):
            first = row[key]
            compact[key] = (
                None
                if first is None
                else {
                    k: first[k]
                    for k in (
                        "layer",
                        "model_layer",
                        "changed",
                        "elements",
                        "max_abs",
                        "rms",
                        "first_difference",
                    )
                }
            )
        print("REUSE_RANK " + json.dumps(compact), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    print_summary(summarize(parser.parse_args().log))
