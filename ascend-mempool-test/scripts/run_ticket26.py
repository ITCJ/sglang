"""Ticket26: two migration regressions and paired multibase kernel measurements."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", choices=("prefill", "decode"), required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--sdk-build-id", required=True)
    p.add_argument("--sdk-source-confirmation", default="")
    p.add_argument("--prefill-host", required=True)
    p.add_argument("--decode-host", required=True)
    p.add_argument(
        "--stage", choices=("all", "phase1", "phase2", "benchmark"), default="all"
    )
    p.add_argument("--control-port", type=int, default=22000)
    p.add_argument("--describe", action="store_true")
    a = p.parse_args()
    repo = Path(__file__).resolve().parents[2]
    root = Path("/home/cryang/log/k2") / a.run_id
    phases = ["phase1", "phase2", "benchmark"] if a.stage == "all" else [a.stage]
    report: dict[str, Any] = dict(
        status="not-run",
        stages={v: "not-run" for v in phases},
        hardware_acceptance="pending user",
        ticket25_unlocked=False,
        model_compute_overlap="unknown; requires same-workload concurrent trace",
        regression="each phase: all four ticket09 geometries, ticket12/18 cases and V1-V10",
    )
    if a.describe:
        print(json.dumps(report, indent=2))
        return 0
    if not a.run_id or Path(a.run_id).name != a.run_id or a.run_id in (".", ".."):
        p.error("run-id must be a new directory name")
    root.mkdir(parents=True, exist_ok=True)
    report_file = root / f"ticket26-{a.role}-{a.stage}.json"
    if report_file.exists():
        p.error(
            "this stage already has a report; use a new run-id or resume another stage"
        )
    child: subprocess.Popen[Any] | None = None

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt(f"ticket26 interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
        nonlocal child
        child = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
        code = child.wait()
        child = None
        if code:
            raise RuntimeError(f"child returned {code}: {command[1]}")

    try:
        for phase in phases:
            checkout = repo
            if phase == "phase1":
                checkout = root / f"phase1-source-{a.role}"
                run(
                    ["git", "clone", "--shared", "--local", str(repo), str(checkout)],
                    repo,
                )
                run(
                    [
                        "git",
                        "checkout",
                        "--detach",
                        "aa5852624e15df139df7c76bb8d757341b049713",
                    ],
                    checkout,
                )
                archive = (
                    repo
                    / ".scratch/k2/verification/ticket26-local/phase1-source.tar.gz"
                )
                with tarfile.open(archive) as bundle:
                    bundle.extractall(checkout)
            env = dict(os.environ)
            if phase != "benchmark":
                config = json.loads(a.config.read_text())
                config.update(observe=True, observe_trace=False)
                generated = root / f"{phase}-{a.role}-config.json"
                generated.write_text(json.dumps(config, indent=2) + "\n")
                run(
                    [
                        sys.executable,
                        "ascend-mempool-test/scripts/run_k1_full_acceptance.py",
                        "--role",
                        a.role,
                        "--config",
                        str(generated),
                        "--run-id",
                        a.run_id + "-" + phase,
                        "--sdk-build-id",
                        a.sdk_build_id,
                        "--page-allocation",
                    ],
                    checkout,
                    env,
                )
                if phase == "phase2" and a.role == "decode":
                    evidence = cache_eviction(
                        Path("/home/cryang/log/mempool-min") / (a.run_id + "-" + phase)
                    )
                    (root / "cache-eviction.json").write_text(
                        json.dumps(evidence, indent=2) + "\n"
                    )
                    if evidence["status"] != "passed":
                        raise RuntimeError(
                            "long generation lacks actual eviction/refill/slot-map evidence"
                        )
            else:
                for ranks, nodes, suffix, port in (
                    ([0, 1], [0], "pair", a.control_port),
                    (list(range(16)), list(range(8)), "tp16", a.control_port + 200),
                ):
                    run(
                        [
                            sys.executable,
                            "ascend-mempool-test/scripts/run_bandwidth.py",
                            "--role",
                            a.role,
                            "--prefill-host",
                            a.prefill_host,
                            "--decode-host",
                            a.decode_host,
                            "--run-id",
                            a.run_id + "-" + suffix,
                            "--suite",
                            "remote-write",
                            "--copy-backend",
                            "multibase",
                            "--sdk-source-confirmation",
                            a.sdk_source_confirmation,
                            "--control-port",
                            str(port),
                            "--devices",
                            *map(str, ranks),
                            "--numa-nodes",
                            *map(str, nodes),
                        ],
                        repo,
                        env,
                    )
            report["stages"][phase] = "passed"
            report_file.write_text(json.dumps(report, indent=2) + "\n")
        report["status"] = "passed_execution_scope"
        return 0
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="failed", error=str(error))
        return 1
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
        report_file.write_text(json.dumps(report, indent=2) + "\n")


def cache_eviction(root: Path) -> dict[str, Any]:
    requests: dict[tuple[Any, ...], dict[str, int]] = {}
    for path in root.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record.get("event") != "materialization":
                continue
            key = (record.get("run_id"), record.get("rid"), record.get("rank"))
            entry = requests.setdefault(
                key,
                dict(
                    max_step=0,
                    evicted_entries=0,
                    refilled_rows=0,
                    slot_map_updates=0,
                    cache_refill_checked_rows=0,
                    cache_refill_errors=0,
                    slot_map_checked_rows=0,
                    slot_map_missing=0,
                    slot_map_stale=0,
                ),
            )
            entry["max_step"] = max(entry["max_step"], record["step"])
            for field in (
                "evicted_entries",
                "refilled_rows",
                "slot_map_updates",
                "cache_refill_checked_rows",
                "cache_refill_errors",
                "slot_map_checked_rows",
                "slot_map_missing",
                "slot_map_stale",
            ):
                entry[field] += record[field]
    errors = [
        dict(run_id=k[0], rid=k[1], rank=k[2], **v)
        for k, v in requests.items()
        if any(
            v[f] for f in ("cache_refill_errors", "slot_map_missing", "slot_map_stale")
        )
    ]
    if errors:
        return dict(
            status="failed",
            requests=errors,
            reason="cache content or slot-map verification failed",
        )
    evidence = [
        dict(run_id=k[0], rid=k[1], rank=k[2], **v)
        for k, v in requests.items()
        if v["max_step"] >= 64
        and all(
            v[f] > 0
            for f in (
                "evicted_entries",
                "refilled_rows",
                "slot_map_updates",
                "cache_refill_checked_rows",
                "slot_map_checked_rows",
            )
        )
        and all(
            v[f] == 0
            for f in ("cache_refill_errors", "slot_map_missing", "slot_map_stale")
        )
    ]
    return dict(
        status="passed" if evidence else "missing",
        requests=evidence,
        token_id_equality="checked by the preceding full service acceptance",
    )


if __name__ == "__main__":
    raise SystemExit(main())
