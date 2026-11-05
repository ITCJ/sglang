"""Build the handoff's standalone binary-v1 extension in the current environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sgl-source", type=Path, required=True)
    p.add_argument(
        "--cann",
        default=os.environ.get("ASCEND_HOME_PATH", "/usr/local/Ascend/cann-9.0.0"),
    )
    p.add_argument("--soc", default="Ascend910_9382")
    p.add_argument("--jobs", type=int, default=8)
    a = p.parse_args()
    source = (
        Path(__file__).resolve().parents[2]
        / "python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase"
    )
    build = source / "build"
    configure = [
        "cmake",
        "-S",
        str(source),
        "-B",
        str(build),
        "-DSGL_SOURCE=" + str(a.sgl_source.resolve()),
        "-DCMAKE_BUILD_TYPE=RELEASE",
        "-DSOC_VERSION=" + a.soc,
        "-DASCEND_HOME_PATH=" + a.cann,
        "-DASCEND_INCLUDE_DIR=" + a.cann + "/aarch64-linux/include",
        "-DASCEND_DRIVER_PATH=/usr/local/Ascend/driver",
    ]
    subprocess.run(configure, check=True)
    subprocess.run(
        [
            "cmake",
            "--build",
            str(build),
            "--target",
            "unidex_multibase",
            "-j",
            str(a.jobs),
        ],
        check=True,
    )
    library = build / "libunidex_multibase.so"
    identity = dict(
        library=str(library),
        sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
        source_sha256={
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("host.cpp", "kernel.cpp", "CMakeLists.txt")
        },
        configure=configure,
        kernel="binary-v1",
        hardware_validation="not-run",
    )
    (build / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    print(json.dumps(identity), flush=True)


if __name__ == "__main__":
    main()
