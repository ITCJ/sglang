"""Run the single-rank and TP16-sized index-K copy matrix on an A3 host."""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


BATCH_SIZES = (1, 2, 4, 8, 11, 16, 32, 64)
WORLD_SIZES = (1, 16)
SOURCE_DIR = Path(__file__).resolve().parent


def environment_int(name, default, minimum):
    value = int(os.environ.get(name, default))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def render_report(report):
    settings = report["settings"]
    cases = {(case["bs"], case["nproc"]): case for case in report["cases"]}
    lines = [
        "# Index K Host-to-NPU transfer sweep",
        "",
        f"Status: **{report['status']}** | UTC: {report['started_utc']} | commit: `{report['commit']}`",
        f"Context: {settings['context_len']} tokens | warmup: {settings['warmup']} | "
        f"samples: {settings['repeats']} | host buffers/rank: {settings['host_buffers']} | "
        f"chunk bytes: {settings['chunk_bytes'] or 'whole layer'}",
        "NUMA binding: none (system policy) | "
        "data validation: off (run the separate --validate preflight)",
        "",
        "| BS | MiB/rank | 1 rank wall mean ms | 1 rank GB/s | "
        "16 ranks slowest wall mean ms | 16 ranks slowest GB/s | 16 ranks aggregate GB/s |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    def cells(case, nproc):
        if case is None:
            return ["-", "-", "-"] if nproc == 16 else ["-", "-"]
        if case["status"] != "ok":
            return ["FAILED", "-", "-"] if nproc == 16 else ["FAILED", "-"]
        summary = case["summary"]
        wall_mean_ms = summary["slowest_rank_wall_mean_ms"]
        mib = summary["bytes_per_layer_per_rank"]
        latency = f"{wall_mean_ms:.3f}"
        slow_gbps = mib / (wall_mean_ms * 1e6)
        if nproc == 16:
            return [latency, f"{slow_gbps:.2f}",
                    f"{summary['aggregate_effective_GBps_estimate']:.2f}"]
        return [latency, f"{slow_gbps:.2f}"]

    for bs in BATCH_SIZES:
        mib = bs * settings["context_len"] * 128 * 2 / 2**20
        one = cells(cases.get((bs, 1)), 1)
        sixteen = cells(cases.get((bs, 16)), 16)
        lines.append(f"| {bs} | {mib:.0f} | " + " | ".join(one + sixteen) + " |")

    lines.extend([
        "",
        "Wall mean is the total time to enqueue and complete all repeats divided "
        "by repeat count, with one synchronization after the loop. The 16-rank "
        "time is the slowest rank's total divided by repeats. GB/s uses decimal "
        "bytes and mean wall time; aggregate GB/s is an estimate based on all copied bytes "
        "and the slowest rank, without accounting for barrier release skew.",
        "This is a contiguous pinned-memory copy with no inference or data validation.",
        "Each worker group allocates buffers for the largest BS once and reuses their prefixes.",
    ])
    if report.get("failure"):
        failure = report["failure"]
        lines.extend([
            "",
            f"Failure: BS={failure['bs']}, ranks={failure['nproc']}, "
            f"exit={failure['exit_code']}; {failure['error']}",
            f"Log: `{failure['log_path']}`",
        ])
    return "\n".join(lines) + "\n"


def main():
    settings = {
        "context_len": environment_int("TARGET_CTX", 65536, 1),
        "warmup": environment_int("TRANSFER_WARMUP", 10, 0),
        "repeats": environment_int("TRANSFER_REPEATS", 50, 1),
        "host_buffers": environment_int("HOST_BUFFERS", 2, 1),
        "chunk_bytes": environment_int("COPY_CHUNK_BYTES", 0, 0),
    }
    results_dir = Path(os.environ.get("RESULTS_DIR", SOURCE_DIR / "results")).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = results_dir / f"transfer_sweep_{stamp}_{os.getpid()}"
    run_dir.mkdir(parents=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR,
                            capture_output=True, text=True, check=True).stdout.strip()
    report = {"status": "running", "started_utc": stamp, "commit": commit,
              "settings": settings, "cases": [], "failure": None}
    report_path = run_dir / "summary.md"
    report_path.write_text(render_report(report))
    print(f"Sweep report: {report_path}", flush=True)

    for nproc in WORLD_SIZES:
        case_dir = run_dir / f"n{nproc}"
        env = os.environ.copy()
        env.update(BS=str(BATCH_SIZES[-1]), NPROC=str(nproc),
                   TARGET_CTX=str(settings["context_len"]),
                   TRANSFER_RUN_DIR=str(case_dir),
                   TRANSFER_LOG_LABEL=f"transfer_sweep_n{nproc}")
        command = ["bash", str(SOURCE_DIR / "run_transfer_bench.sh"),
                   "--batch-sizes", *(str(bs) for bs in BATCH_SIZES)]
        print(f"Running all batch sizes with ranks={nproc}", flush=True)
        completed = None
        failed_bs = BATCH_SIZES[0]
        try:
            completed = subprocess.run(command, cwd=SOURCE_DIR, env=env, check=False)
            for bs in BATCH_SIZES:
                failed_bs = bs
                summary_path = case_dir / f"bs{bs}_summary.json"
                if not summary_path.is_file():
                    break
                summary = json.loads(summary_path.read_text())
                if (summary.get("status") != "ok"
                        or summary.get("measurement_protocol") != "batched_async_sync_once_v1"
                        or summary.get("world_size") != nproc
                        or summary.get("batch_size") != bs
                        or summary.get("config", {}).get("context_len") != settings["context_len"]
                        or summary.get("validation_enabled") is not False
                        or summary.get("correct") is not None):
                    raise RuntimeError(f"inconsistent unvalidated summary for BS={bs}")
                report["cases"].append({"bs": bs, "nproc": nproc, "status": "ok",
                                        "summary": summary})
                report_path.write_text(render_report(report))
            if completed.returncode != 0 or len([c for c in report["cases"] if c["nproc"] == nproc]) != len(BATCH_SIZES):
                raise RuntimeError(f"worker group exited {completed.returncode} before completing all batch sizes")
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
            log_file = case_dir / "log_path.txt"
            log_path = log_file.read_text().strip() if log_file.is_file() else "unavailable"
            if not any(c["bs"] == failed_bs and c["nproc"] == nproc for c in report["cases"]):
                report["cases"].append({"bs": failed_bs, "nproc": nproc, "status": "failed"})
            report["status"] = "stopped" if isinstance(exc, KeyboardInterrupt) else "failed"
            report["failure"] = {"bs": failed_bs, "nproc": nproc,
                                 "exit_code": completed.returncode if completed is not None else "not started",
                                 "error": repr(exc), "log_path": log_path}
            report_path.write_text(render_report(report))
            print(f"Sweep stopped: {report_path}", flush=True)
            return 130 if isinstance(exc, KeyboardInterrupt) else 1

    report["status"] = "ok"
    report_path.write_text(render_report(report))
    print(f"TRANSFER_SWEEP_OK: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
