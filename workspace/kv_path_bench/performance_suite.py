#!/usr/bin/env python3
"""Run a smoke check and all five capacities x two layouts x three Host paths."""

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
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        # Only kill this runner's child process group, never other NPU jobs.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise


def save_summary(result, run_dir):
    encoded = json.dumps(result, indent=2) + "\n"
    (run_dir / "summary.json").write_text(encoded)
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow(("tokens", "layout", "path", "median_ms", "p95_ms", "effective_gbps"))
    for case in result["cases"]:
        if case["smoke"]:
            continue
        for path in case["result"]["paths"]:
            writer.writerow((case["tokens"], case["layout"], path["path"],
                             path["median_s"] * 1000, path["p95_s"] * 1000,
                             path["effective_gbps"]))
    (run_dir / "summary.csv").write_text(table.getvalue())


def run_suite(args, run_dir, run_case=execute_case):
    with (run_dir / "cli.log").open("w") as logfile:
        with redirect_stdout(Tee(sys.stdout, logfile)), redirect_stderr(Tee(sys.stderr, logfile)):
            return _run_suite(args, run_dir, run_case)


def _run_suite(args, run_dir, run_case):
    result = {"status": "running", "measurement_protocol": "whole_request_v2",
              "run_dir": str(run_dir), "validation_enabled": args.validate,
              "preflight_validation_requested": args.preflight_validate,
              "preflight": None, "cases": []}
    save_summary(result, run_dir)
    script = Path(__file__).with_name("kv_transfer_bench.py")
    for index, (tokens, layout, smoke, preflight) in enumerate(cases(args.preflight_validate)):
        prefix = "preflight-" if preflight else ""
        output = run_dir / f"{prefix}{tokens}-{layout}.json"
        log = run_dir / f"{prefix}{tokens}-{layout}.log"
        case_validate = True if preflight else args.validate
        command = [
            sys.executable, str(script), args.client_ip, args.store_ip,
            "--tokens", str(tokens), "--layout", layout,
            "--device", str(args.device), "--output", str(output), "--log", str(log),
            "--warmup", "0" if smoke or preflight else str(args.warmup),
            "--repeats", "1" if smoke or preflight else str(args.repeats),
        ]
        if case_validate:
            command.append("--validate")
        print(f"Running kind={'preflight' if preflight else 'performance'} tokens={tokens} layout={layout}", flush=True)
        failure = "F9"
        try:
            rc, terminal = run_case(command, args.timeout)
            # Keep unexpected native output in a file, never flood the operator.
            (run_dir / f"{tokens}-{layout}-terminal.log").write_text(terminal)
            if rc != 0:
                codes = [line for line in terminal.splitlines() if line in {"F2", "F3", "F5", "F9"}]
                failure = codes[-1] if codes else "F9"
                raise RuntimeError(f"child exit={rc}")
            data = json.loads(output.read_text())
            if (data.get("measurement_protocol") != "whole_request_v2"
                    or data.get("status") != "ok" or data.get("tokens") != tokens
                    or data.get("layout") != layout
                    or [path["path"] for path in data.get("paths", [])] != [PATH_NAMES[c] for c in "ACM"]
                    or not all(path.get("validation_enabled") is case_validate
                               and "correct" in path
                               and path["correct"] is (True if case_validate else None)
                               for path in data["paths"])):
                raise RuntimeError("missing or invalid successful result")
            if preflight:
                result["preflight"] = {"status": "ok", "tokens": tokens,
                                       "layout": layout, "result": data}
            else:
                result["cases"].append({"tokens": tokens, "layout": layout,
                                        "smoke": smoke, "result": data})
            for path in data["paths"]:
                print(f"tokens={tokens} layout={layout} {path['path']}={path['median_s'] * 1000:.3f} ms", flush=True)
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                failure = "STOP"
            elif isinstance(exc, subprocess.TimeoutExpired):
                failure = "TIMEOUT"
            result.update(status="stopped" if failure == "STOP" else "failed",
                          failed_case=index, failed_kind="preflight" if preflight else "performance",
                          failed_tokens=tokens, failed_layout=layout,
                          failure_code=failure, error=repr(exc))
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
    parser.add_argument("client_ip")
    parser.add_argument("store_ip")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--validate", action="store_true", help="validate all KV bytes after every iteration, including smoke (default: off)")
    parser.add_argument("--preflight-validate", action="store_true",
                        help="validate one 4K scattered case before the unvalidated performance matrix")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=1800, help="maximum seconds per configuration")
    args = parser.parse_args()
    if args.validate and args.preflight_validate:
        parser.error("--validate and --preflight-validate are mutually exclusive")
    if args.device < 0 or args.warmup < 0 or args.repeats < 1 or args.timeout <= 0:
        parser.error("invalid device, warmup, repeats or timeout")
    run_dir = RESULTS / datetime.now().strftime("%y%m%d_%H%M%S")
    # Never overwrite a previous run, including two starts in the same second.
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_suite(args, run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
