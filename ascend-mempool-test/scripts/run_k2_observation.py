"""Ticket13 paired acceptance: four geometries, off/on, shared fixtures and two rounds."""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import msgspec
from run_k1_full_acceptance import MATRIX, configurations

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ascend_mempool_pd.mempool_router_protocol import RouterConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sdk-build-id", required=True)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="one geometry only; does not satisfy full acceptance",
    )
    parser.add_argument("--geometry", choices=[f"b{b}-sd{sd}" for b, sd in MATRIX])
    parser.add_argument("--observe", choices=("both", "on", "off"), default="both")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="explicitly enable profiler capture and graph trace gate",
    )
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,45}", args.run_id):
        parser.error("run-id must be a new simple name, at most 46 characters")
    configs = configurations(RouterConfig.read(str(args.config)), args.sdk_build_id)
    if args.smoke and args.geometry:
        parser.error("--smoke and --geometry cannot be combined")
    if args.geometry:
        configs = [(name, c) for name, c in configs if name == args.geometry]
    if args.smoke:
        configs = configs[:1]
    modes = (False, True) if args.observe == "both" else (args.observe == "on",)
    coverage = (
        "selected"
        if args.geometry or args.observe != "both"
        else ("smoke" if args.smoke else "full")
    )
    root = Path("/home/cryang/log/k2") / args.run_id
    if args.describe:
        print(
            json.dumps(
                dict(
                    coverage=coverage,
                    configurations={
                        name: msgspec.to_builtins(c) for name, c in configs
                    },
                    target_rounds=2,
                    observation_modes=list(modes),
                    trace_enabled=args.trace,
                    warmup_round=1,
                    normal_target_starts_per_side=2 * len(modes) * len(configs),
                    logs=str(root),
                ),
                indent=2,
            )
        )
        return 0
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / f"report-{args.role}.json"
    if report_path.exists():
        parser.error("run-id already has a report")
    report = dict(
        status="failed",
        overhead=[],
        coverage=coverage,
        observation_modes=list(modes),
        off_on_comparison="pending" if len(modes) == 2 else "not-run",
        user_accepted=False,
        stages=[],
        target_rounds=2,
        warmup_round=1,
    )
    child = None

    def stop(signum, frame):
        raise KeyboardInterrupt(f"stopped by signal {signum}")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        report["trace_enabled"] = args.trace
        report["graph_trace_gate"] = "not-run"
        if args.trace:
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("verify_observation_graph.py")),
                    "--log-dir",
                    str(root / f"graph-{args.role}"),
                ]
            )
            if child.wait():
                raise RuntimeError(
                    "Graph observation gate failed; service was not started"
                )
            child = None
            report["graph_trace_gate"] = "passed"
        for name, base in configs:
            responses = {}
            walls = {}
            for enabled in modes:
                label = f"{name}-{'on' if enabled else 'off'}"
                config = msgspec.structs.replace(
                    base, observe=enabled, observe_trace=enabled and args.trace
                )
                generated = root / f"{label}-{args.role}.json"
                generated.write_bytes(msgspec.json.encode(config))
                child_id = f"{args.run_id}-{label}"
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("run_mempool_min.py")),
                    "--role",
                    args.role,
                    "--config",
                    str(generated),
                    "--mode",
                    "full-acceptance",
                    "--run-id",
                    child_id,
                    "--log-root",
                    str(root),
                    "--observation-rounds",
                    "2",
                ]
                child = subprocess.Popen(command)
                code = child.wait()
                child = None
                folder = root / child_id / args.role
                report["stages"].append(
                    dict(label=label, exit_code=code, root=str(folder))
                )
                report_path.write_text(json.dumps(report, indent=2))
                if code:
                    raise RuntimeError(f"{label} functional acceptance failed")
                if args.role == "prefill":
                    response = json.loads(
                        (folder / "target/responses.json").read_text()
                    )
                    responses[enabled] = [r["output_ids"] for r in response["results"]]
                    walls[enabled] = [
                        r["observation_request_wall_ns"] for r in response["results"]
                    ]
                    fork = json.loads(
                        (folder / "target/observation-fork.json").read_text()
                    )["result"]
                    responses[enabled].append(fork["output_ids"])
                    walls[enabled].append(fork["observation_request_wall_ns"])
                if enabled:
                    analysis = json.loads(
                        (folder / "target/observation.json").read_text()
                    )
                    report["stages"][-1]["observation_status"] = analysis["status"]
                    report["stages"][-1]["missing"] = analysis["missing"]
                    if args.role == "prefill":
                        from analyze_k2_observation import compare_copy_bytes

                        functional = json.loads((folder / "v1-v10.json").read_text())
                        byte_checks = compare_copy_bytes(
                            analysis["copy_bytes"], functional["k1"]["shared"]["writes"]
                        )
                        report["stages"][-1]["copy_byte_checks"] = byte_checks
                        if not byte_checks or any(not r["passed"] for r in byte_checks):
                            raise ValueError(
                                f"{label}: observation/functional receipt bytes differ"
                            )
                        peer_analysis = json.loads(
                            (
                                folder / "target/decode-observation-checks.json"
                            ).read_text()
                        )
                        report["stages"][-1]["decode_observation_status"] = (
                            peer_analysis["status"]
                        )
                        report["stages"][-1]["decode_missing"] = peer_analysis[
                            "missing"
                        ]
                        if peer_analysis["status"] != "passed":
                            raise ValueError(
                                f"{label}: decode observation evidence incomplete"
                            )
                    if analysis["status"] != "passed":
                        raise ValueError(f"{label}: observation evidence incomplete")
                elif list(folder.rglob("*.jsonl")):
                    raise ValueError("observation files created with the switch off")
            if len(responses) == 2 and responses[False] != responses[True]:
                raise ValueError(f"{name}: off/on output token IDs differ")
            if len(walls) == 2:
                report["off_on_comparison"] = "passed"
                report["overhead"].append(
                    dict(
                        geometry=name,
                        n=1,
                        warmup=1,
                        request_wall_off_ns=walls[False],
                        request_wall_on_ns=walls[True],
                        delta_ns=[
                            on - off for off, on in zip(walls[False], walls[True])
                        ],
                        note="matched serial cases; one measured round; includes profiler/collection/filesystem effects",
                    )
                )
        report["status"] = "passed_current_configuration"
        report["external_pending"] = [
            "user k/2k/4k cache-capacity logs and ticket acceptance confirmation"
        ]
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc)
        return 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    raise SystemExit(main())
