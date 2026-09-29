#!/usr/bin/env python3
"""Run smoke and five capacities x two mappings with the selected copy engine."""

import argparse
import csv
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from path_names import PATH_NAMES
from unidex_engine import HOST_MEMORY, PATH_NAME


CAPACITIES = (1024, 4096, 16384, 65536, 131072)
LAYOUTS = ("contiguous", "scattered")
RESULTS = Path(__file__).resolve().parent / "results"


class Tee:
    def __init__(self, terminal, logfile):
        self.terminal, self.logfile = terminal, logfile

    def write(self, text):
        self.terminal.write(text)
        self.logfile.write(text)
        self.logfile.flush()
        return len(text)

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()


def cases(preflight_validate=False):
    preflight = [(4096, "scattered", False, True)] if preflight_validate else []
    return preflight + [(128, "contiguous", True, False)] + [
        (tokens, layout, False, False) for tokens in CAPACITIES for layout in LAYOUTS
    ]


def execute_case(command, timeout):
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        print(f"CHILD_PID={process.pid} COMMAND={json.dumps(command)}", flush=True)
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
        # Only kill this runner's child process group, never other NPU jobs.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
        exc.output = output
        exc.returncode = process.returncode
        raise


def save_summary(result, run_dir):
    encoded = json.dumps(result, indent=2) + "\n"
    (run_dir / "summary.json").write_text(encoded)
    table = io.StringIO()
    writer = csv.writer(table)
    columns = ("tokens", "layout", "path", "median_ms", "p95_ms", "effective_gbps")
    unidex = result.get("engine") == "unidex"
    writer.writerow(columns + (("engine", "host_memory", "validation_enabled", "block_dim") if unidex else ()))
    for case in result["cases"]:
        if case["smoke"]:
            continue
        for path in case["result"]["paths"]:
            row = (case["tokens"], case["layout"], path["path"],
                   path["median_s"] * 1000, path["p95_s"] * 1000, path["effective_gbps"])
            if unidex:
                row += (path["engine"], path["host_memory"], path["validation_enabled"], path["block_dim"])
            writer.writerow(row)
    (run_dir / "summary.csv").write_text(table.getvalue())


def run_suite(args, run_dir, run_case=execute_case):
    with (run_dir / "cli.log").open("w") as logfile:
        with redirect_stdout(Tee(sys.stdout, logfile)), redirect_stderr(Tee(sys.stderr, logfile)):
            return _run_suite(args, run_dir, run_case)


def _run_suite(args, run_dir, run_case):
    engine = getattr(args, "copy_engine", "sglkernel")
    unidex = engine == "unidex"
    expected_paths = [PATH_NAME] if unidex else [PATH_NAMES[c] for c in "ACM"]
    result = {"status": "running", "measurement_protocol": "whole_request_v2",
              "run_dir": str(run_dir), "validation_enabled": args.validate,
              "preflight_validation_requested": args.preflight_validate,
              "preflight": None, "cases": []}
    if unidex:
        result.update(engine=engine, host_memory=HOST_MEMORY, l2_only=True, block_dim=args.block_dim,
                      actual_command=[sys.executable, *sys.argv], working_directory=os.getcwd())
    save_summary(result, run_dir)
    script = Path(__file__).with_name("kv_transfer_bench.py")
    for index, (tokens, layout, smoke, preflight) in enumerate(cases(args.preflight_validate)):
        prefix = "preflight-" if preflight else ""
        output = run_dir / f"{prefix}{tokens}-{layout}.json"
        log = run_dir / f"{prefix}{tokens}-{layout}.log"
        case_validate = True if preflight else args.validate
        command = [sys.executable, str(script)]
        if unidex:
            command += ["--copy-engine", "unidex", "--l2-only", "--block-dim", str(args.block_dim)]
        else:
            command += [args.client_ip, args.store_ip]
        command += [
            "--tokens", str(tokens), "--layout", layout,
            "--device", str(args.device), "--output", str(output), "--log", str(log),
            "--warmup", "0" if smoke or preflight else str(args.warmup),
            "--repeats", "1" if smoke or preflight else str(args.repeats),
        ]
        if case_validate:
            command.append("--validate")
        print(f"Running kind={'preflight' if preflight else 'performance'} tokens={tokens} layout={layout}", flush=True)
        failure = "F9"
        rc = None
        try:
            rc, terminal = run_case(command, args.timeout)
            # Keep unexpected native output in a file, never flood the operator.
            (run_dir / f"{prefix}{tokens}-{layout}-terminal.log").write_text(terminal)
            if rc != 0:
                codes = [line for line in terminal.splitlines() if line in {"F2", "F3", "F5", "F9"}]
                failure = codes[-1] if codes else "F9"
                raise RuntimeError(f"child exit={rc}")
            data = json.loads(output.read_text())
            if (data.get("measurement_protocol") != "whole_request_v2"
                    or data.get("status") != "ok" or data.get("tokens") != tokens
                    or data.get("layout") != layout
                    or [path["path"] for path in data.get("paths", [])] != expected_paths
                    or (unidex and (data.get("engine") != engine
                                    or data.get("host_memory") != HOST_MEMORY
                                    or data.get("l2_only") is not True
                                    or data.get("block_dim") != args.block_dim))
                    or not all(path.get("validation_enabled") is case_validate
                               and "correct" in path
                               and path["correct"] is (True if case_validate else None)
                               for path in data["paths"])):
                raise RuntimeError("missing or invalid successful result")
            if preflight:
                result["preflight"] = {"status": "ok", "tokens": tokens,
                                       "layout": layout, "command": command, "exit_code": rc, "result": data}
            else:
                result["cases"].append({"tokens": tokens, "layout": layout,
                                        "smoke": smoke, "command": command, "exit_code": rc, "result": data})
            for path in data["paths"]:
                print(f"tokens={tokens} layout={layout} {path['path']}={path['median_s'] * 1000:.3f} ms", flush=True)
        except (Exception, KeyboardInterrupt) as exc:
            interrupted_output = getattr(exc, "output", None)
            if interrupted_output is not None:
                if isinstance(interrupted_output, bytes):
                    interrupted_output = interrupted_output.decode(errors="replace")
                (run_dir / f"{prefix}{tokens}-{layout}-terminal.log").write_text(interrupted_output)
            if isinstance(exc, KeyboardInterrupt):
                failure = "STOP"
            elif isinstance(exc, subprocess.TimeoutExpired):
                failure = "TIMEOUT"
            result.update(status="stopped" if failure == "STOP" else "failed",
                          failed_case=index, failed_kind="preflight" if preflight else "performance",
                          failed_tokens=tokens, failed_layout=layout,
                          failure_code=failure, error=repr(exc), failed_command=command,
                          failed_exit_code=getattr(exc, "returncode", rc),
                          stage="child whole-request execution/result collection")
            save_summary(result, run_dir)
            print(f"X{index} {failure}", flush=True)
            return 130 if failure == "STOP" else 1
        save_summary(result, run_dir)
    result["status"] = "ok"
    save_summary(result, run_dir)
    print("ALL_OK", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_ip", nargs="?")
    parser.add_argument("store_ip", nargs="?")
    parser.add_argument("--copy-engine", choices=("sglkernel", "unidex"), default="sglkernel")
    parser.add_argument("--l2-only", action="store_true")
    parser.add_argument("--block-dim", type=int, default=24)
    parser.add_argument("--results-dir", type=Path)
    parser.add_argument("--image-digest", default="unknown")
    parser.add_argument("--kernel-source-dir", type=Path)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--validate", action="store_true", help="validate all KV bytes after every iteration, including smoke (default: off)")
    parser.add_argument("--preflight-validate", action="store_true",
                        help="validate one 4K scattered case before the unvalidated performance matrix")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=1800, help="maximum seconds per configuration")
    args = parser.parse_args()
    if args.copy_engine == "unidex" and not args.l2_only:
        parser.error("unidex requires --l2-only; no L3 mode")
    if args.l2_only and args.copy_engine != "unidex":
        parser.error("--l2-only requires --copy-engine unidex")
    if not args.l2_only and (not args.client_ip or not args.store_ip):
        parser.error("provide client and Store IPs")
    if args.validate and args.preflight_validate:
        parser.error("--validate and --preflight-validate are mutually exclusive")
    if (args.device < 0 or args.warmup < 0 or args.repeats < 1 or args.timeout <= 0
            or not 1 <= args.block_dim <= (1 << 32) - 1):
        parser.error("invalid device, warmup, repeats or timeout")
    results = args.results_dir or (Path(__file__).resolve().parents[1] / "unidex_copy_bench/results"
                                   if args.copy_engine == "unidex" else RESULTS)
    run_dir = results / datetime.now().strftime("%y%m%d_%H%M%S")
    # Never overwrite a previous run, including two starts in the same second.
    run_dir.mkdir(parents=True, exist_ok=False)
    print(f"RUN_DIR={run_dir}", flush=True)
    if args.copy_engine == "unidex":
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "unidex_copy_bench/capture_environment.py"),
                   "--output", str(run_dir / "environment.json"), "--image-digest", args.image_digest]
        if args.kernel_source_dir:
            command += ["--kernel-source-dir", str(args.kernel_source_dir)]
        with (run_dir / "environment.log").open("w") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        if completed.returncode != 0:
            (run_dir / "summary.json").write_text(json.dumps(dict(status="failed", stage="environment capture",
                                                                exit_code=completed.returncode)) + "\n")
            print(f"ENV_FAIL log={run_dir / 'environment.log'}", flush=True)
            return 1
    return run_suite(args, run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
