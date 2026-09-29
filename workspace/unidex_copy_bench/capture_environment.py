#!/usr/bin/env python3
"""Record explicit target versions without dumping environment variables."""

import argparse
from importlib import metadata
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys


def git_version(path):
    if path is None:
        return dict(commit="unknown", tracked_changes="unknown")
    manifest = path / "unidex-source-manifest/source_commit.txt"
    if manifest.is_file():
        return dict(commit=manifest.read_text().strip(), tracked_changes="unknown",
                    source="offline manifest")
    try:
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
    except OSError:
        head = None
    if head is not None and head.returncode == 0:
        dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True)
        return dict(commit=head.stdout.strip(),
                    tracked_changes=bool(dirty.stdout.strip()) if dirty.returncode == 0 else "unknown")
    return dict(commit="unknown", tracked_changes="unknown")


def version_files(paths):
    for path in paths:
        if path.is_file():
            lines = [line.strip() for line in path.read_text(errors="replace").splitlines()
                     if line.lower().startswith(("version=", "version:", "version ", "cann_version="))]
            if lines:
                return dict(file=str(path), version_lines=lines)
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-digest", default="unknown")
    parser.add_argument("--kernel-source-dir", type=Path)
    args = parser.parse_args()
    versions = {}
    for package in ("torch", "torch-npu", "sgl-kernel-npu", "memfabric-hybrid", "mooncake-transfer-engine",
                    "wheel", "setuptools", "pybind11", "pip"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unknown"
    repo = Path(__file__).resolve().parents[2]
    sources = ("workspace/kv_path_bench/kv_transfer_bench.py",
               "workspace/kv_path_bench/unidex_engine.py",
               "workspace/kv_path_bench/performance_suite.py",
               "workspace/unidex_copy_bench/capture_environment.py",
               "workspace/unidex_copy_bench/run.py",
               "workspace/unidex_copy_bench/correctness.sh",
               "workspace/fabric_direct_bench/check.py",
               "workspace/fabric_direct_bench/performance.py",
               "workspace/fabric_direct_bench/unidex_bm.py")
    cann_home = Path(os.environ.get("ASCEND_HOME_PATH", "/usr/local/Ascend/ascend-toolkit/latest"))
    if cann_home.name == "set_env.sh":
        cann_home = cann_home.parent
    try:
        ipc_namespace = os.readlink("/proc/self/ns/ipc")
    except OSError:
        ipc_namespace = "unknown"
    record = dict(
        python=sys.version, python_executable=sys.executable,
        actual_command=[sys.executable, *sys.argv], working_directory=os.getcwd(),
        environment_collection_only=True, target_library_imports=False, npu_operations=False,
        ipc_namespace=ipc_namespace,
        architecture=platform.machine(), os=platform.platform(),
        packages=versions, image_digest=args.image_digest,
        sglang_repository=git_version(repo),
        benchmark_files_sha256={name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
                                if (repo / name).is_file() else "unknown" for name in sources},
        kernel_repository=git_version(args.kernel_source_dir),
        cann=version_files([
            cann_home / "version.cfg",
            cann_home / "aarch64-linux/ascend_toolkit_install.info",
        ]),
        driver=version_files([Path("/usr/local/Ascend/driver/version.info")]),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
