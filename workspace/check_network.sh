#!/usr/bin/env bash
# G=GitHub, P=configured pip index, A=configured apt repositories.
# 0=reachable, H=HTTP error, N=network/TLS error, T=timeout,
# C=configuration error, X=tool missing, -=not checked.
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
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--require", choices=("all", "apt", "pip"), default="all")
args = parser.parse_args()

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

def probe(url):
    if not shutil.which("curl"):
        return "X"
    if urlsplit(url).scheme not in ("https", "http"):
        return "C"
    try:
        result = command(["curl", "--silent", "--location", "--fail",
                          "--connect-timeout", "3", "--max-time", "6",
                          "--proto", "=http,https", "--proto-redir", "=http,https",
                          "--output", os.devnull, url])
        return {0: "0", 22: "H", 28: "T"}.get(result.returncode, "N")
    except subprocess.TimeoutExpired:
        return "T"
    except OSError:
        return "X"

groups = {"G": ["https://github.com"], "P": [], "A": []}
states = {}
for label, resolver in (("P", pip_urls), ("A", apt_urls)):
    if (args.require == "apt" and label == "P") or (args.require == "pip" and label == "A"):
        states[label] = "-"
        continue
    try:
        groups[label] = resolver()
    except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired):
        states[label] = "C"

fd, path = tempfile.mkstemp(prefix="mooncake-network-", suffix=".log")
with os.fdopen(fd, "w") as log, concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    pending = [(label, url, pool.submit(probe, url))
               for label, urls in groups.items() for url in urls]
    for label, url, future in pending:
        state = future.result()
        # Do not record URL credentials or query strings from private mirrors.
        log.write(f"{label} {urlsplit(url).hostname} {state}\n")
        if states.get(label, "0") == "0":
            states[label] = state
    report = "NET:" + "".join(label + states.get(label, "C") for label in "GPA")
    log.write(report + "\n")
print(report)
required = {"all": "GPA", "apt": "A", "pip": "P"}[args.require]
raise SystemExit(int(any(states.get(label) != "0" for label in required)))
PY
