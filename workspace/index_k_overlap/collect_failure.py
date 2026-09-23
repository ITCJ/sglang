"""Collect existing experiment evidence without contacting the inference server."""

import argparse
from collections import deque
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def report(output, manifest, archive):
    """Print a bounded summary; preserve full evidence in the archive."""
    def emit(label, value):
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x1f\x7f]", " ", str(value))
        line = f"{label}: {value}"
        print(line if len(line) <= 120 else line[:117] + "...")

    def read(name):
        path = output / name
        return path.read_text(errors="replace") if path.is_file() else ""

    try:
        summary = json.loads(read("decode/summary.json") or "{}")
    except (ValueError, OSError):
        summary = {}
    emit("UTC", manifest["collected_utc"])
    emit("RUN", Path(manifest["sources"].get("decode_dir", "missing")).name)
    emit("SERVER", Path(manifest["sources"].get("server_dir", "missing")).name)
    emit("STATUS", summary.get("status", "summary missing"))
    emit("ERROR", summary.get("error", "none recorded"))
    emit("PREP seconds", summary.get("input_preparation_seconds", "not recorded"))
    versions = manifest["packages"]
    emit("VERSIONS", " ".join(f"{key}={versions[key]}" for key in ("aiohttp", "sglang", "uvicorn")))
    commit = read("git_version.txt").splitlines()
    emit("COMMIT", commit[1] if len(commit) > 1 else "unavailable")
    tokens = summary.get("final_tokens", [])
    details = summary.get("request_details", [])
    print("REQ  STAGE       HTTP  TOKENS  HEADERS(s)  FIRST_TOKEN(s)")
    for i in range(min(max(len(tokens), len(details)), 11)):
        item = details[i] if i < len(details) else {}
        def elapsed(key):
            if key in item and "sent_unix_time" in item:
                return f"{item[key] - item['sent_unix_time']:.3f}"
            return "-"
        print(f"{i:>3}  {item.get('stage', 'unknown'):<11} "
              f"{str(item.get('http_status', '-')):>4}  "
              f"{str(tokens[i] if i < len(tokens) else '-'):>6}  "
              f"{elapsed('headers_unix_time'):>10}  {elapsed('first_token_unix_time'):>14}")
    failed = [item for item in details if item.get("error_repr")]
    if failed:
        first = min(failed, key=lambda item: item.get("ended_unix_time", float("inf")))
        emit("FIRST FAILURE", f"req={first.get('request_index')} {first['error_repr']}")
        emit("CAUSE/CONTEXT", f"{first.get('cause')} / {first.get('context')}")
    else:
        emit("REQUEST ERRORS", summary.get("request_errors", "not recorded"))
    events = read("server_events.log").splitlines()
    batches = [line for line in events if "Decode batch" in line]
    emit("LAST DECODE", batches[-1] if batches else "not found")
    faults = [line for line in events if re.search(r"error|exception|out.of.memory|state was deleted", line, re.I)]
    for line in faults[-2:]:
        emit("SERVER ERROR", line)
    emit("WARNINGS", " | ".join(manifest["warnings"]) or "none")
    emit("SAVED", f"results/{Path(archive).name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--logs-dir", required=True)
    parser.add_argument("--decode-dir", type=Path, help="Override latest decode result directory")
    parser.add_argument("--server-dir", type=Path, help="Override latest server result directory")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    results = Path(args.results_dir).resolve()
    logs = Path(args.logs_dir).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = results / f"diagnostics_{stamp}_{os.getpid()}"
    output.mkdir(parents=True)
    manifest = {"collected_utc": stamp, "warnings": [], "sources": {},
                "python": sys.version, "executable": sys.executable,
                "note": "Latest server/decode selected independently; verify timestamps. No live server probe performed."}

    def warn(message):
        manifest["warnings"].append(message)

    def latest(paths):
        candidates = list(paths)
        return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

    def command(name, argv):
        try:
            result = subprocess.run(argv, cwd=source, capture_output=True, text=True,
                                    errors="replace", timeout=15)
            (output / name).write_text(
                f"exit_code={result.returncode}\n{result.stdout}\n{result.stderr}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{name}: {exc!r}")

    def log_excerpt(path, label):
        manifest["sources"][label + "_log"] = str(path)
        # Keep the end plus the most recent diagnostic lines across the whole log.
        tail = deque(maxlen=1500)
        matches = deque(maxlen=1000)
        pattern = re.compile(
            r"traceback|error|exception|out.of.memory|retract|abort|disconnect|"
            r"state was deleted|POST /generate|POST /start_profile|POST /stop_profile|"
            r"#running-req|profiling|keep.alive", re.I)
        with path.open(errors="replace") as stream:
            with (output / f"{label}_head.log").open("w") as head:
                for number, line in enumerate(stream, 1):
                    entry = f"{number}: {line}"
                    if number <= 150:
                        head.write(entry)
                    tail.append(entry)
                    if pattern.search(line):
                        matches.append(entry)
        (output / f"{label}_tail.log").write_text("".join(tail))
        (output / f"{label}_events.log").write_text("".join(matches))

    decode = args.decode_dir or latest(p for p in results.glob("decode_*") if p.is_dir())
    server = args.server_dir or latest(
        p for p in results.iterdir() if p.is_dir() and
        (p.name.startswith("server_") or p.name.startswith("incremental_stage")))
    for label, directory, log_patterns in (
        ("decode", decode, ("decode_*.log",)),
        ("server", server, ("server_*.log", "incremental_stage*.log")),
    ):
        try:
            log_path = None
            if directory is not None and directory.is_dir():
                directory = directory.resolve()
                manifest["sources"][label + "_dir"] = str(directory)
                destination = output / label
                destination.mkdir()
                for name in ("summary.json", "server_info.json", "progress.jsonl", "preflight.json",
                             "command.txt", "log_path.txt", "server.sh", "server_cli_help.txt"):
                    path = directory / name
                    if path.is_file():
                        if path.stat().st_size <= 16 * 1024 * 1024:
                            shutil.copy2(path, destination / name)
                        else:
                            warn(f"Skipped oversized metadata file: {path}")
                pointer = directory / "log_path.txt"
                if pointer.is_file():
                    log_path = Path(pointer.read_text().strip())
                if label == "decode" and not (directory / "summary.json").exists():
                    warn("Decode summary missing: client may still be running or exited abruptly.")
            else:
                warn(f"No {label} result directory found: {directory}")
            if log_path is None or not log_path.is_file():
                warn(f"{label}: using latest log fallback; verify it belongs to the failed run.")
                log_path = latest(p for pattern in log_patterns for p in logs.glob(pattern) if p.is_file())
            if log_path is not None:
                log_excerpt(log_path, label)
            else:
                warn(f"No {label} log found")
        except (OSError, ValueError) as exc:
            warn(f"{label}: {exc!r}")

    command("git_version.txt", ["git", "log", "-1", "--format=%H %D%n%s"])
    command("git_status.txt", ["git", "status", "--short"])
    command("experiment_diff.patch", ["git", "diff", "HEAD", "--", "."])
    manifest["packages"] = {}
    for package in ("aiohttp", "sglang", "torch", "torch-npu", "transformers", "uvicorn", "mooncake-transfer-engine"):
        try:
            manifest["packages"][package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            manifest["packages"][package] = "not registered"
    code = output / "source"
    code.mkdir()
    for path in source.iterdir():
        if path.suffix in (".py", ".sh"):
            shutil.copy2(path, code / path.name)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    archive = shutil.make_archive(str(output), "gztar", root_dir=output.parent, base_dir=output.name)
    report(output, manifest, archive)


if __name__ == "__main__":
    main()
