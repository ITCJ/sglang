#!/usr/bin/env python3
"""Combine the newest summary.csv from the three KV benchmark suites."""

import argparse
import csv
from pathlib import Path


SUITES = (
    ("hicache_l2", Path("workspace/hicache_l2_bench/results")),
    ("mooncake", Path("workspace/kv_path_bench/results")),
    ("memfabric", Path("workspace/fabric_direct_bench/results")),
)
OUTPUT_COLUMNS = (
    "experiment",
    "run_id",
    "tokens",
    "smoke",
    "layout",
    "path",
    "metric",
    "median_ms",
    "p95_ms",
    "effective_gbps",
    "source_csv",
)


def latest_summary(repo: Path, relative_results: Path) -> Path:
    results = repo / relative_results
    if not results.is_dir():
        raise FileNotFoundError(f"results directory does not exist: {results}")
    runs = sorted(path for path in results.iterdir() if path.is_dir())
    if not runs:
        raise FileNotFoundError(f"no result runs found in: {results}")
    summary = runs[-1] / "summary.csv"
    if not summary.is_file():
        raise FileNotFoundError(
            f"latest result run has no summary.csv: {runs[-1]}"
        )
    return summary


def read_rows(experiment: str, summary: Path, repo: Path) -> list[dict[str, str]]:
    with summary.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"tokens", "path", "median_ms", "p95_ms"}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{summary} is missing required columns: {sorted(missing)}"
            )
        rows = []
        for source in reader:
            rows.append(
                {
                    "experiment": experiment,
                    "run_id": summary.parent.name,
                    "tokens": source.get("tokens", ""),
                    "smoke": source.get("smoke", ""),
                    "layout": source.get("layout", ""),
                    "path": source.get("path", ""),
                    "metric": source.get("metric", "total_s"),
                    "median_ms": source.get("median_ms", ""),
                    "p95_ms": source.get("p95_ms", ""),
                    "effective_gbps": source.get("effective_gbps", ""),
                    "source_csv": str(summary.relative_to(repo)),
                }
            )
    return rows


def collect(repo: Path) -> tuple[list[dict[str, str]], list[Path]]:
    rows = []
    sources = []
    for experiment, relative_results in SUITES:
        summary = latest_summary(repo, relative_results)
        sources.append(summary)
        rows.extend(read_rows(experiment, summary, repo))
    return rows, sources


def main() -> int:
    default_repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=default_repo,
        help="repository root (default: inferred from this script)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_repo / "workspace/latest_bench_summary.csv",
        help="combined CSV output path",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    rows, sources = collect(repo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    for source in sources:
        print(f"SOURCE {source.relative_to(repo)}")
    print(f"OUTPUT {args.output.resolve()} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
