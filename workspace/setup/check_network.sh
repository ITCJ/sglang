#!/usr/bin/env bash
# Human-readable endpoint checks with retries for intermittent connectivity.
# Probe failures block only the repository required by the calling installer.
set -euo pipefail
"${PYTHON_BIN:-python3}" - "$@" <<'PY'
import argparse
import ast
import concurrent.futures
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import re
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--require", choices=("all", "apt", "pip"), default="all")
parser.add_argument("--attempts", type=int, default=3)
parser.add_argument("--timeout", type=int, default=20)
args = parser.parse_args()
if args.attempts < 1 or args.timeout < 1:
    parser.error("--attempts and --timeout must be positive")

fd, path = tempfile.mkstemp(prefix="mooncake-network-", suffix=".log")
log = os.fdopen(fd, "w")
output_lock = threading.Lock()

def emit(message):
    with output_lock:
        print(message, flush=True)
        log.write(message + "\n")
        log.flush()

def safe_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or "<invalid-host>"
    if ":" in host:
        host = "[" + host + "]"
    return parsed.scheme + "://" + host + (":" + str(parsed.port) if parsed.port else "") + parsed.path

def safe_error(error):
    return re.sub(r"https?://[^\s\"']+", lambda match: safe_url(match.group()), error).strip()

def command(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10)

def pip_urls():
    config = command([sys.executable, "-m", "pip", "config", "list"])
    if config.returncode:
        raise ValueError("pip config unavailable")
    values = {}
    for line in config.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = ast.literal_eval(value)
    if os.environ.get("PIP_NO_INDEX", "").lower() in ("1", "true", "yes"):
        raise ValueError("offline pip configuration")
    index = os.environ.get("PIP_INDEX_URL") or values.get("install.index-url") or values.get("global.index-url") or "https://pypi.org/simple"
    extra = os.environ.get("PIP_EXTRA_INDEX_URL") or values.get("install.extra-index-url") or values.get("global.extra-index-url") or ""
    urls = [base.rstrip("/") + "/mooncake-transfer-engine-npu/"
            for base in [index, *shlex.split(extra)]]
    return urls

def apt_urls():
    # Let apt parse both sources.list and deb822 sources, including architecture.
    result = command(["apt-get", "--print-uris", "update"])
    if result.returncode:
        raise ValueError("apt sources unavailable")
    urls = []
    for line in result.stdout.splitlines():
        if line.startswith("'"):
            url = shlex.split(line)[0]
            if urlsplit(url).scheme in ("http", "https"):
                urls.append(url)
    if not urls:
        raise ValueError("no HTTP apt sources")
    # InRelease requests cover the configured repositories without downloading
    # large Packages indexes. Keep separate suites of the same mirror.
    releases = [url for url in urls if url.endswith(("/InRelease", "/Release"))]
    return list(dict.fromkeys(releases or urls))

def probe(label, url):
    target = f"[{label}] {safe_url(url)}"
    if not shutil.which("curl"):
        emit(f"{target}: curl is not installed")
        return "X"
    if urlsplit(url).scheme not in ("https", "http"):
        emit(f"{target}: unsupported URL scheme")
        return "C"
    state = "N"
    for attempt in range(1, args.attempts + 1):
        emit(f"{target}: attempt {attempt}/{args.attempts}")
        try:
            result = subprocess.run(
                ["curl", "--silent", "--show-error", "--location", "--fail",
                 "--connect-timeout", str(min(10, args.timeout)),
                 "--max-time", str(args.timeout),
                 "--proto", "=http,https", "--proto-redir", "=http,https",
                 "--write-out", "%{http_code} %{time_total}",
                 "--output", os.devnull, url],
                capture_output=True, text=True, timeout=args.timeout + 2)
            state = {0: "0", 22: "H", 28: "T"}.get(result.returncode, "N")
            if state == "0":
                suffix = " (succeeded after retry; connection may be unstable)" if attempt > 1 else ""
                emit(f"{target}: OK; HTTP/time(s): {result.stdout.strip()}{suffix}")
                return state
            emit(f"{target}: FAILED; curl exit={result.returncode}; HTTP/time(s): {result.stdout.strip()}; {safe_error(result.stderr)}")
        except subprocess.TimeoutExpired:
            state = "T"
            emit(f"{target}: timed out after {args.timeout + 2}s")
        except OSError as exc:
            emit(f"{target}: could not execute curl: {exc}")
            return "X"
        if attempt < args.attempts:
            time.sleep(1)
    emit(f"{target}: all {args.attempts} attempts failed; this does not prove permanent unavailability")
    return state

groups = {"G": ["https://github.com"], "P": [], "A": []}
states = {}
names = {"G": "GitHub", "P": "pip index", "A": "Ubuntu repositories"}
emit(f"Network check: up to {args.attempts} attempts per URL, {args.timeout}s per attempt.")
emit("GitHub is tested directly; git pull may use a different proxy or remote URL.")
for label, resolver in (("P", pip_urls), ("A", apt_urls)):
    if (args.require == "apt" and label == "P") or (args.require == "pip" and label == "A"):
        states[label] = "-"
        continue
    try:
        groups[label] = resolver()
    except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired) as exc:
        states[label] = "C"
        emit(f"[{names[label]}] Could not read repository configuration ({type(exc).__name__}).")

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    pending = [(label, url, pool.submit(probe, names[label], url))
               for label, urls in groups.items() for url in urls]
    for label, url, future in pending:
        state = future.result()
        if states.get(label, "0") == "0":
            states[label] = state
descriptions = {"0": "reachable", "H": "HTTP error", "N": "network or TLS failure",
                "T": "timed out", "C": "configuration unavailable",
                "X": "required tool missing", "-": "not checked"}
emit("\nSummary:")
for label in "GPA":
    emit(f"  {names[label]}: {descriptions[states.get(label, 'C')]}")
emit("A successful check does not guarantee subsequent package downloads will succeed.")
emit(f"Log: {path}")
required = {"all": "GPA", "apt": "A", "pip": "P"}[args.require]
if args.require != "all" and states.get(required) != "0":
    emit("Installation stopped because its required repository check failed.")
log.close()
raise SystemExit(int(any(states.get(label) != "0" for label in required)))
PY
