"""Render only measured data; retain unknown placement and overlap conclusions."""

from __future__ import annotations

import argparse
import json
import importlib
from pathlib import Path
from statistics import median
from typing import Any

from ascend_mempool.bandwidth import BenchmarkConfig, build_plan
from ascend_mempool.bandwidth_report import validate_measurements


def validate_versions(
    environments: dict[str, Any], source_confirmation: str = ""
) -> dict[str, Any]:
    values = list(environments.values())
    for key in ("kernel_binaries", "torch", "torch_npu", "unidex_schema", "memfabric"):
        observed = [e.get(key) for e in values]
        if not observed or not observed[0] or any(v != observed[0] for v in observed):
            raise ValueError(f"worker runtime identity differs or is missing: {key}")
    builds = {}
    for host in ("local", "peer"):
        observed = [
            e.get("sdk_binaries")
            for path, e in environments.items()
            if ("peer" if path.startswith("peer/") else "local") == host
        ]
        if not observed or not observed[0] or any(v != observed[0] for v in observed):
            raise ValueError(f"sdk_binaries missing or different within host: {host}")
        builds[host] = observed[0]
    differs = builds["local"] != builds["peer"]
    if differs and not source_confirmation.strip():
        raise ValueError(
            "sdk_binaries differ across hosts: source confirmation required"
        )
    return dict(
        status="source_confirmed_separate_builds" if differs else "identical_binaries",
        source_confirmation=source_confirmation,
        builds=builds,
        source_evidence="user_confirmation" if source_confirmation else "not_supplied",
    )


def finish_report(
    root: Path, args: argparse.Namespace, output: Path | None = None
) -> None:
    output = root if output is None else output
    report = json.loads((output / "report.json").read_text())
    config = BenchmarkConfig(
        **{**report["config"], "nodes": tuple(report["config"]["nodes"])}
    )
    rows = [r for p in root.glob("**/samples.json") for r in json.loads(p.read_text())]
    pipes = [
        r for p in root.glob("**/pipeline.json") for r in json.loads(p.read_text())
    ]
    validate_measurements(config, rows, pipes)
    evidence = {
        str(p.relative_to(root)): json.loads(p.read_text())
        for p in root.glob("**/environment.json")
    }
    expected = {
        f"{prefix}rank-{rank}/environment.json"
        for prefix in ("", "peer/")
        for rank in range(config.tp_size)
    }
    if set(evidence) != expected:
        raise ValueError("missing or unexpected worker version evidence")
    report["sdk_identity"] = validate_versions(
        evidence, report.get("sdk_source_confirmation", "")
    )
    environments = list(evidence.values())
    measurements = report["measurements"]
    for name, row in measurements.items():
        if row["min_overlap_fraction"] <= 0:
            raise ValueError(f"{name}: readers/writers had no common measured interval")
    residency = [
        r for p in root.glob("**/residency.json") for r in json.loads(p.read_text())
    ]
    report.update(
        status="passed_measurement_scope",
        residency=residency,
        physical_numa_verified=bool(residency)
        and all(r["placement_verified"] for r in residency),
        pipeline=pipes,
        versions=environments[0],
        user_accepted=False,
    )
    conclusions = [
        "# Ticket14 measured conclusions",
        "SDK identity: " + json.dumps(report["sdk_identity"], ensure_ascii=False),
        "",
        f"Scope: {config.suite}. This is not model-service or NPU user acceptance.",
        "Timing: eager host submission through actual completion, including Python/SDK overhead.",
        "Each rank uses its own host clock; aggregation only combines active ranks on one host.",
        "Logical KV bytes are distinct from physical traffic including replicas and Index K.",
        "Local/other NUMA are relative to rank 0; multi-reader cases span the configured rank topology.",
        "",
        "## Remote writes and dual writes",
        "",
    ]
    for n in (4096, 8192, 32768, 131072):
        for target in ("remote", "dual"):
            name = f"write-{n}-{target}"
            if name not in measurements:
                continue
            row = measurements[name]
            latency = row["first_group_completion_median_ms"]
            judgement = "unknown: no same-machine compute window supplied"
            if args.layer_window_ms is not None:
                judgement = (
                    "first synchronized group operation fits the supplied window in isolation"
                    if latency < args.layer_window_ms
                    else "exceeds the supplied window in isolation"
                )
            conclusions.append(
                f"- {name}: {row['median_GBps']:.3f} GB/s, first-group completion median {latency:.3f} ms; {judgement}."
            )
    background = [
        r for p in root.glob("**/background.json") for r in json.loads(p.read_text())
    ]
    report["background"] = background
    baseline = measurements.get("d-local-read-baseline")
    if baseline:
        from ascend_mempool.bandwidth_report import percentile

        by_case: dict[str, list[float]] = {}
        for row in background:
            if row["case"].endswith("-dread"):
                by_case.setdefault(row["case"], []).extend(row["durations_ms"])
        for name, durations in by_case.items():
            if not durations:
                raise ValueError(
                    f"{name}: no background read timings in measurement window"
                )
            p99 = percentile(durations, 0.99)
            conclusions.append(
                f"- D read under {name}: p99 {p99:.3f} ms; no-write baseline {baseline['p99_ms']:.3f} ms; ratio {p99 / baseline['p99_ms']:.3f}x. Window includes control-boundary lag."
            )
    conclusions += [
        "",
        "Layer overlap is NOT proven. Unpaced pipeline samples show queueing and final drain, not model compute overlap.",
        "",
        "## U6 / Q24 / Q7 / Q5",
        "",
    ]
    for side in (0, 1):
        for location in ("local", "remote"):
            for grain in ("token", "layer", "page"):
                curve = [
                    (
                        n,
                        measurements[f"read-{side}-{location}-{grain}-{n}"][
                            "median_GBps"
                        ],
                    )
                    for n in (1, 2, 4, 8, 16)
                    if f"read-{side}-{location}-{grain}-{n}" in measurements
                ]
                if not curve:
                    continue
                one = curve[0][1]
                # A declared descriptive convention, never a pass/fail threshold.
                linear = [n for n, rate in curve if rate >= 0.8 * n * one]
                saturated = next(
                    (
                        n
                        for (prev_n, prev), (n, rate) in zip(curve, curve[1:])
                        if rate < 0.8 * (n / prev_n) * prev
                    ),
                    None,
                )
                prefix = f"{side}-{location}-{grain}"
                conclusions.append(
                    f"- {prefix}: >=80% of ideal linear throughput through {max(linear)} readers; first sublinear step {saturated}. Raw curve: {curve}."
                )
                stripe = measurements.get(f"striped-{prefix}")
                if stripe and curve[-1][0] == config.tp_size:
                    conclusions.append(
                        f"  Q5: interleaved segments / single segment = {stripe['median_GBps'] / curve[-1][1]:.3f}x, same bytes per reader."
                    )
                if len(curve) >= 3:
                    conclusions.append(
                        f"  Q7: four-reader per-reader throughput / two-reader = {(curve[2][1] / 4) / (curve[1][1] / 2):.3f}x."
                    )
    if config.suite != "full":
        conclusions.append("U6/Q24/Q7/Q5 full matrix remains unmeasured in this scope.")
    for side in (0, 1):
        for grain in ("token", "layer", "page"):
            a, b = f"read-{side}-local-{grain}-1", f"read-{side}-remote-{grain}-1"
            if a in measurements and b in measurements:
                conclusions.append(
                    f"- {side}/{grain}: remote/local throughput = {measurements[b]['median_GBps'] / measurements[a]['median_GBps']:.3f}x."
                )
    conclusions += [
        "",
        "## Q27 / small reads",
        "",
        "Our p99 is a complete 2048-slice gather step. Mooncake additionally records individual get p99.",
        "Mooncake get ends in CPU bytes; our gather ends in NPU HBM. Do not call this an equal-endpoint transport comparison.",
    ]
    mooncake = report.get("mooncake", {})
    if mooncake.get("status") != "measured":
        conclusions.append(
            "Mooncake unavailable/not requested; Q27 comparison remains unknown. See mooncake.json for deployment evidence."
        )
    moon_medians: dict[str, Any] = next(
        (
            v["medians"]
            for v in mooncake.values()
            if isinstance(v, dict) and v.get("medians")
        ),
        {},
    )
    report["small_read_comparison"] = []
    for case in build_plan(config):
        if case.group == "small_read" and case.name in measurements:
            selected = [r for r in rows if r["case"] == case.name]
            rates = [
                2048 * len(r["durations_ms"]) / ((r["done_ns"] - r["start_ns"]) / 1e9)
                for r in selected
            ]
            measurements[case.name]["slice_ops_per_second"] = median(rates)
            conclusions.append(
                f"- {case.name}: {median(rates):.1f} slices/s, step p99 {measurements[case.name]['p99_ms']:.3f} ms, CPU cores {measurements[case.name]['median_cpu_cores']:.3f}."
            )
    conclusions += [
        "",
        "| Slice B | NPU gather slices/s | CPU get ops/s | NPU CPU cores | get CPU cores | NPU step p99 ms | get step p99 ms |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for size in (1152, 4096, 16384, 147456):
        own = measurements.get(f"small-1-{size}")
        other = moon_medians.get(str(size))
        if own:
            entry = dict(slice_bytes=size, npu=own, mooncake=other)
            report["small_read_comparison"].append(entry)
            m = other or dict(
                ops_per_second="unknown", cpu_cores="unknown", step_p99_ms="unknown"
            )
            conclusions.append(
                f"| {size} | {own['slice_ops_per_second']:.1f} | {m['ops_per_second']} | {own['median_cpu_cores']:.3f} | {m['cpu_cores']} | {own['p99_ms']:.3f} | {m['step_p99_ms']} |"
            )
    conclusions += [
        "",
        "## Physical residency",
        f"Observed mappings: {len(residency)}; all verified: {report['physical_numa_verified']}.",
        "Unknown driver page counts remain unknown; NUMA configuration is not placement proof.",
    ]
    report["pipeline_medians"] = {
        str(rank): {
            key: median(r[key] for r in pipes if r["rank"] == rank)
            for key in ("total_ms", "tail_drain_ms", "retained_source_bytes")
        }
        for rank in range(config.tp_size)
    }
    (output / "conclusions.md").write_text("\n".join(conclusions) + "\n")
    plot(output, report, config)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def plot(root: Path, report: dict[str, Any], config: BenchmarkConfig) -> None:
    matplotlib = importlib.import_module("matplotlib")
    matplotlib.use("Agg")
    plt = importlib.import_module("matplotlib.pyplot")
    groups: dict[str, list[str]] = {}
    for case in build_plan(config):
        if case.name in report["measurements"]:
            groups.setdefault(case.group, []).append(case.name)
    for group, names in groups.items():
        if group == "readers":
            fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
            for side in (0, 1):
                for column, grain in enumerate(("token", "layer", "page")):
                    ax = axes[side, column]
                    for location in ("local", "remote"):
                        points = [
                            (
                                n,
                                report["measurements"][
                                    f"read-{side}-{location}-{grain}-{n}"
                                ]["median_GBps"],
                            )
                            for n in (1, 2, 4, 8, 16)
                            if f"read-{side}-{location}-{grain}-{n}"
                            in report["measurements"]
                        ]
                        if points:
                            ax.plot(
                                [p[0] for p in points],
                                [p[1] for p in points],
                                "o-",
                                label=location,
                            )
                        stripe = report["measurements"].get(
                            f"striped-{side}-{location}-{grain}"
                        )
                        if stripe:
                            ax.scatter(
                                [config.tp_size],
                                [stripe["median_GBps"]],
                                marker="^",
                                label=location + " striped",
                            )
                    ax.set(
                        title=f"side {side}, {grain}", xlabel="readers", ylabel="GB/s"
                    )
                    ax.grid(alpha=0.25)
                    ax.legend(fontsize=7)
            fig.savefig(root / "readers.svg")
            plt.close(fig)
            continue
        fig, axes = plt.subplots(
            2, 1, figsize=(max(10, len(names) * 0.22), 8), layout="constrained"
        )
        for ax, metric, unit in zip(
            axes, ("median_GBps", "p99_ms"), ("GB/s", "completion p99 (ms)")
        ):
            ax.plot(
                range(len(names)),
                [report["measurements"][name][metric] for name in names],
                "o-",
            )
            ax.set_ylabel(unit)
            ax.set_xticks(range(len(names)), names, rotation=90, fontsize=6)
            ax.grid(alpha=0.25)
        fig.suptitle(f"Ticket14 {group}: actual eager completion samples")
        fig.savefig(root / f"{group}.svg")
        plt.close(fig)

    if report.get("small_read_comparison"):
        fig, axes = plt.subplots(1, 3, figsize=(12, 4), layout="constrained")
        for ax, own_key, moon_key, unit in zip(
            axes,
            ("slice_ops_per_second", "median_cpu_cores", "p99_ms"),
            ("ops_per_second", "cpu_cores", "step_p99_ms"),
            ("slice ops/s", "CPU cores", "2048-slice step p99 ms"),
        ):
            for backend, key in (("npu", own_key), ("mooncake", moon_key)):
                selected = [
                    row for row in report["small_read_comparison"] if row.get(backend)
                ]
                if selected:
                    ax.plot(
                        [row["slice_bytes"] for row in selected],
                        [row[backend][key] for row in selected],
                        "o-",
                        label=backend,
                    )
            ax.set(xscale="log", xlabel="slice bytes", ylabel=unit)
            ax.legend()
            ax.grid(alpha=0.25)
        fig.suptitle("Different endpoints: NPU HBM gather versus CPU bytes get")
        fig.savefig(root / "small_read_comparison.svg")
        plt.close(fig)
    if report.get("pipeline_medians"):
        fig, ax = plt.subplots(figsize=(8, 4), layout="constrained")
        values = report["pipeline_medians"]
        ax.plot(
            list(values),
            [v["total_ms"] for v in values.values()],
            "o-",
            label="all layers",
        )
        ax.plot(
            list(values),
            [v["tail_drain_ms"] for v in values.values()],
            "o-",
            label="final drain",
        )
        ax.set(
            xlabel="writer rank",
            ylabel="median ms",
            title="Unpaced writes, no model overlap claim",
        )
        ax.legend()
        fig.savefig(root / "pipeline.svg")
        plt.close(fig)
