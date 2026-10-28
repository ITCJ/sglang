"""Recompute derived measurements from the user's returned CSV extracts.

Standard library only. Does not access NPU services or change acceptance status.
The original on-host report generator remains mempool_perf_report.py.
"""

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    table = {}
    with (root / "metrics.csv").open() as stream:
        for row in csv.DictReader(stream):
            key = row["metric"], row["case"]
            if key in table:
                raise ValueError(f"duplicate metric: {key}")
            table[key] = {
                k: int(v) if k == "n" else float(v)
                for k, v in row.items()
                if k not in ("metric", "case") and v
            }
    requests, groups = defaultdict(dict), defaultdict(list)
    with (root / "round-samples.csv").open() as stream:
        for row in csv.DictReader(stream):
            number, case, metric, value = (
                int(row["round"]),
                row["case"],
                row["metric"],
                float(row["value_ms"]),
            )
            if metric in requests[number, case]:
                raise ValueError(f"duplicate round sample: {row}")
            requests[number, case][metric] = value
            groups[metric, case].append(value)
    if set(requests) != {(number, case) for number in range(2, 6) for case in "ABCE"}:
        raise ValueError("expected four measured ABCE rounds")
    for key, values in groups.items():
        if len(values) != 4:
            raise ValueError(f"incomplete samples: {key}")
        if key in table:
            for field, reduce in (
                ("p50", statistics.median),
                ("min", min),
                ("max", max),
            ):
                if abs(reduce(values) - table[key][field]) >= 1e-6:
                    raise ValueError(
                        f"summary differs from round samples: {key}/{field}"
                    )
    summary = {}
    keys = (
        "T_main_e2e",
        "T_route",
        "T_ready",
        "T_gen",
        "T_tail",
        "residual",
        "T_main_queue_wait",
    )
    for case in "ABCE":
        rows = [requests[number, case] for number in range(2, 6)]
        for row in rows:
            row["residual"] = row["T_main_e2e"] - sum(
                row[key] for key in ("T_route", "T_ready", "T_gen", "T_tail")
            )
        means = {key: statistics.mean(row[key] for row in rows) for key in keys}
        e2e = table["T_main_e2e", case]
        means.update(
            e2e_spread_percent=100 * (e2e["max"] - e2e["min"]) / e2e["p50"],
            gen_percent=100 * means["T_gen"] / means["T_main_e2e"],
            queue_percent=100 * means["T_main_queue_wait"] / means["T_main_e2e"],
            residual_range=[
                min(r["residual"] for r in rows),
                max(r["residual"] for r in rows),
            ],
        )
        summary[case] = means
    unstable = [
        dict(
            metric=metric,
            case=case,
            ratio=row["max"] / row["min"],
            spread_ms=row["max"] - row["min"],
            spread_e2e_percent=100
            * (row["max"] - row["min"])
            / table["T_main_e2e", case]["p50"],
        )
        for (metric, case), row in table.items()
        if metric != "T_step" and row["max"] / row["min"] > 1.5
    ]
    result = dict(
        summary=summary,
        unstable=unstable,
        rounds=[
            dict(round=number, case=case, **row)
            for (number, case), row in requests.items()
        ],
    )
    (root / "derived.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"Validated {len(table)} summary rows and {sum(map(len, groups.values()))} round samples; wrote derived.json"
    )


if __name__ == "__main__":
    main()
