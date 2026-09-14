#!/usr/bin/env python3
"""Check benchmark prerequisites without importing libraries or opening an NPU."""

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCH = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("store", "client"))
    args = parser.parse_args()

    commit = subprocess.run(
        ["git", "log", "-1", "--oneline"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode:
        print(f"git commit: FAIL ({commit.stderr.strip()})")
        return 1
    print(f"git commit: {commit.stdout.strip()}")

    required = ("torch", "torch_npu", "mooncake")
    if args.role == "client":
        required += ("sgl_kernel_npu",)
    missing = []
    for name in ("torch", "torch_npu", "mooncake", "sgl_kernel_npu"):
        found = importlib.util.find_spec(name) is not None
        print(f"{name}: {'OK' if found else 'MISSING'}")
        if not found and name in required:
            missing.append(name)
    master_found = shutil.which("mooncake_master") is not None
    print(f"mooncake_master: {'OK' if master_found else 'MISSING'}")
    if args.role == "store" and not master_found:
        missing.append("mooncake_master")

    check = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(BENCH), "-p", "test_*.py"],
        cwd=ROOT,
        check=False,
    )
    if check.returncode or missing:
        print("PRECLAIM_FAIL: fix missing dependencies or layout tests before claiming devices")
        return 1
    print("PRECLAIM_OK: no NPU or Mooncake Store was initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
