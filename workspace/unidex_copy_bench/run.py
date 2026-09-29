#!/usr/bin/env python3
"""One source/client entry for exactly three UNIDEX KV paths."""
import argparse
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
SYSV = "L2-L1_unidex_sysv_registered"
BM_PATHS = {"L2-L1_UNIDEX_bm_host", "L3-L1_UNIDEX_bm_remote_host"}
CAPACITIES = (1024, 4096, 16384, 65536, 131072)
LAYOUTS = ("contiguous", "scattered")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def run_stage(command, stage, directory, status, *, leaf_timeout=None):
    status.update(stage=stage, command=command, exit_code=None)
    write_json(directory / "status.json", status)
    print(f"STAGE={stage} COMMAND={json.dumps(command)}", flush=True)
    # Each existing runner owns and reaps its own worker process groups.
    # Keep it alive on interruption until it has propagated cancellation.
    with (directory / f"{stage}.log").open("w") as log:
        child = subprocess.Popen(command, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
        def relay():
            for line in child.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
        thread = threading.Thread(target=relay)
        thread.start()
        try:
            rc = child.wait(timeout=leaf_timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            # Ignore repeated interrupts while the existing runner reaps workers.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if leaf_timeout is not None:
                # Direct KV workers have no separately sessioned children.
                # TERM invokes their synchronization and cleanup handler.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    rc = child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    rc = child.wait()
            else:
                # Existing Python supervisors reap their own worker sessions.
                child.send_signal(signal.SIGINT)
                rc = child.wait()
            status["exit_code"] = rc
            raise
        finally:
            thread.join()
            child.stdout.close()
    status["exit_code"] = rc
    write_json(directory / "status.json", status)
    if rc:
        raise RuntimeError(f"{stage} exited with {rc}; see {stage}.log")


def check_sysv(args, directory, status):
    checks = directory / "sysv-check"
    checks.mkdir()
    record = dict(status="running")
    write_json(checks / "correctness-status.json", record)
    try:
        for tokens, layout in ((128, "contiguous"), (4096, "scattered")):
            name = f"{tokens}-{layout}"
            record["stage"] = name
            run_stage([sys.executable, str(WORKSPACE / "kv_path_bench/kv_transfer_bench.py"),
                       "--copy-engine", "unidex", "--l2-only", "--device", str(args.device),
                       "--block-dim", str(args.block_dim), "--tokens", str(tokens), "--layout", layout,
                       "--warmup", "0", "--repeats", "1", "--validate",
                       "--output", str(checks / f"{name}.json"), "--log", str(checks / f"{name}.log")],
                      f"sysv-check-{name}", directory, status, leaf_timeout=args.sysv_timeout)
            data = json.loads((checks / f"{name}.json").read_text())
            if (data.get("status") != "ok" or data.get("tokens") != tokens or data.get("layout") != layout
                    or [path["path"] for path in data.get("paths", [])] != [SYSV]
                    or not all(path.get("validation_enabled") is True and path.get("correct") is True
                               for path in data["paths"])):
                raise RuntimeError("invalid SysV correctness result")
        record.update(status="ok", exit_code=0)
    except (Exception, KeyboardInterrupt) as exc:
        record.update(status="failed", exit_code=status.get("exit_code"), error=repr(exc))
        raise
    finally:
        write_json(checks / "correctness-status.json", record)


def collect(directory, check_only, skip_sysv=False):
    selected_paths = BM_PATHS if skip_sysv else BM_PATHS | {SYSV}
    bm = json.loads((directory / "bm/summary.json").read_text())
    bm_status = json.loads((directory / "bm/status.json").read_text())
    if bm_status.get("status") != "ok":
        raise RuntimeError("BM worker did not complete successfully")
    bm_cases = bm["cases"]
    if {case["path"] for case in bm_cases} != BM_PATHS:
        raise RuntimeError("unexpected BM path selection")
    checks = json.loads((directory / "sysv-check/correctness-status.json").read_text()) if (directory / "sysv-check").exists() else None
    if checks is not None and checks.get("status") != "ok":
        raise RuntimeError("SysV correctness did not complete successfully")
    if check_only:
        rows = list(bm_cases)
        for name in (() if skip_sysv else ("128-contiguous", "4096-scattered")):
            data = json.loads((directory / "sysv-check" / f"{name}.json").read_text())
            if data.get("status") != "ok":
                raise RuntimeError("invalid SysV correctness result")
            rows.extend(dict(path, tokens=data["tokens"], layout=data["layout"])
                        for path in data["paths"])
        if ({row["path"] for row in rows} != selected_paths
                or not all(row.get("validation_enabled") is True and row.get("correct") is True
                           for row in rows)):
            raise RuntimeError("missing successful validation for selected paths")
        expected = {(n, layout, path) for n, layout in ((128, "contiguous"), (4096, "scattered"))
                    for path in selected_paths}
        actual = [(row["tokens"], row["layout"], row["path"]) for row in rows]
        if len(actual) != len(expected) or set(actual) != expected:
            raise RuntimeError("incomplete selected-path correctness matrix")
    else:
        sysv = {"cases": []}
        if not skip_sysv:
            summaries = list((directory / "sysv").glob("*/summary.json"))
            if len(summaries) != 1:
                raise RuntimeError("expected exactly one SysV run under this run directory")
            sysv = json.loads(summaries[0].read_text())
            if sysv.get("status") != "ok":
                raise RuntimeError("SysV suite did not complete successfully")
        rows = [case for case in bm_cases if not case["smoke"] and not case.get("preflight", False)]
        for case in sysv["cases"]:
            if not case["smoke"]:
                rows.extend(dict(path, tokens=case["tokens"], layout=case["layout"])
                            for path in case["result"]["paths"])
        expected = {(n, layout, path) for n in CAPACITIES for layout in LAYOUTS
                    for path in selected_paths}
        actual = [(row["tokens"], row["layout"], row["path"]) for row in rows]
        if len(actual) != len(expected) or set(actual) != expected:
            raise RuntimeError("incomplete or duplicate selected-path performance matrix")
        if not all(row.get("validation_enabled") is False and row.get("correct") is None
                   for row in rows):
            raise RuntimeError("performance validation unexpectedly enabled")
    write_json(directory / "summary.json", dict(status="ok", mode="correctness" if check_only else "performance",
               measurement_protocol="whole_request_v2", selected_paths=sorted(selected_paths),
               skipped_paths=[SYSV] if skip_sysv else [], cases=rows))
    with (directory / "summary.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(("tokens", "layout", "path", "median_ms", "p95_ms", "effective_gbps",
                         "validation_enabled", "correct"))
        for row in rows:
            writer.writerow((row["tokens"], row["layout"], row["path"], row["median_s"] * 1000,
                             row["p95_s"] * 1000, row["effective_gbps"],
                             row["validation_enabled"], row["correct"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("source", "client"))
    parser.add_argument("local_ip")
    parser.add_argument("source_ip", nargs="?")
    parser.add_argument("--skip-sysv", action="store_true", help="run only BM local and remote UNIDEX; explicitly record SysV as skipped")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--block-dim", type=int, choices=(24, 48), default=24)
    parser.add_argument("--image-digest", default="unknown", help="optional environment annotation")
    parser.add_argument("--kernel-source-dir", type=Path, help="optional source provenance; uses the installed kernel package")
    parser.add_argument("--results-dir", type=Path, default=HERE / "results/three_paths")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true", help="validate small cases only; no performance matrix")
    mode.add_argument("--preflight-validate", action="store_true", help="validate small cases before each backend's performance matrix")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--bm-timeout", type=int, default=3600, help="seconds for the BM worker including peer wait")
    parser.add_argument("--sysv-timeout", type=int, default=1800, help="seconds per SysV performance case")
    args = parser.parse_args()
    if args.role == "client" and (not args.source_ip or args.source_ip == args.local_ip):
        parser.error("client requires its IP and a distinct source IP")
    if args.role == "source" and args.source_ip:
        parser.error("source takes only its own IP")
    if min(args.device, args.warmup) < 0 or min(args.repeats, args.bm_timeout, args.sysv_timeout) < 1:
        parser.error("invalid device, warmup, repeats or timeout")
    if args.kernel_source_dir is not None and not args.kernel_source_dir.is_dir():
        parser.error("kernel source directory does not exist")
    directory = args.results_dir.resolve() / (datetime.now().strftime("%y%m%d_%H%M%S_%f") + "-" + args.role)
    directory.mkdir(parents=True, exist_ok=False)
    print(f"RUN_DIR={directory}", flush=True)
    status = dict(status="running", role=args.role, actual_command=[sys.executable, *sys.argv],
                  working_directory=os.getcwd(), local_ip=args.local_ip, source_ip=args.source_ip,
                  check_only=args.check_only, preflight_validate=args.preflight_validate,
                  skipped_paths=[SYSV] if args.skip_sysv else [],
                  stage="environment", exit_code=None)
    write_json(directory / "status.json", status)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    source_args = (["--kernel-source-dir", str(args.kernel_source_dir.resolve())]
                   if args.kernel_source_dir is not None else [])
    try:
        run_stage([sys.executable, str(HERE / "capture_environment.py"),
                   "--output", str(directory / "environment.json"),
                   *source_args,
                   "--image-digest", args.image_digest], "environment", directory, status)
        command = [sys.executable, str(WORKSPACE / "fabric_direct_bench/check.py"),
                   args.role, args.local_ip]
        if args.role == "client":
            command.append(args.source_ip)
        command += ["--performance", "--unidex-only", "--device", str(args.device),
                    "--block-dim", str(args.block_dim), "--warmup", str(args.warmup),
                    "--repeats", str(args.repeats), "--timeout", str(args.bm_timeout),
                    "--run-dir", str(directory / "bm")]
        if args.check_only:
            command.append("--unidex-check-only")
        elif args.preflight_validate:
            command.append("--preflight-validate")
        run_stage(command, "bm", directory, status)
        if args.role == "client":
            # BM process and mappings are gone before SysV allocation starts.
            if not args.skip_sysv and (args.check_only or args.preflight_validate):
                check_sysv(args, directory, status)
            if not args.skip_sysv and not args.check_only:
                run_stage([sys.executable, str(WORKSPACE / "kv_path_bench/performance_suite.py"),
                           "--copy-engine", "unidex", "--l2-only", "--device", str(args.device),
                           "--block-dim", str(args.block_dim), "--warmup", str(args.warmup),
                           "--repeats", str(args.repeats), "--timeout", str(args.sysv_timeout),
                           *source_args,
                           "--image-digest", args.image_digest, "--results-dir", str(directory / "sysv")],
                          "sysv", directory, status)
            status["stage"] = "collect"
            if args.skip_sysv:
                print("SYSV_SKIPPED: requested by --skip-sysv", flush=True)
            collect(directory, args.check_only, args.skip_sysv)
        status.update(status="ok", stage="complete", exit_code=0)
        write_json(directory / "status.json", status)
        print("UNIDEX_SOURCE_DONE" if args.role == "source" else
              "UNIDEX_BM_CHECK_OK" if args.skip_sysv and args.check_only else
              "UNIDEX_BM_OK" if args.skip_sysv else
              "UNIDEX_CHECK_OK" if args.check_only else "UNIDEX_ALL_OK", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        stopped = isinstance(exc, KeyboardInterrupt)
        status.update(status="stopped" if stopped else "failed", error=repr(exc))
        write_json(directory / "status.json", status)
        print(f"UNIDEX_FAIL stage={status['stage']} exit_code={status.get('exit_code')} error={exc!r}", flush=True)
        return 130 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
