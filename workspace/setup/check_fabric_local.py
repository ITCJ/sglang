#!/usr/bin/env python3
"""Offline, single-node Ascend Store probe. No model or existing master used.

References:
https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/design/transfer-engine/ascend_direct_transport.md
https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/getting_started/quick-start.md

Local put/get may use a local copy path: success never proves remote HCCS.
Installed wheel API docs and loaded libraries are retained for version auditing.
"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid


def mark(directory, stage):
    (directory / "stage").write_text(stage)
    print(stage, flush=True)


def worker(args):
    directory = Path(args.output)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    mark(directory, "IMPORT")
    import torch
    import torch_npu  # noqa: F401
    from mooncake.store import MooncakeDistributedStore

    print("Mooncake:", importlib.metadata.version("mooncake-transfer-engine-npu"))
    print("torch:", torch.__version__)
    print("setup API:", MooncakeDistributedStore.setup.__doc__)
    print("put API:", MooncakeDistributedStore.put.__doc__)
    print("get API:", MooncakeDistributedStore.get.__doc__)
    mark(directory, "CONFIG")
    if not Path("/etc/hccn.conf").is_file():
        raise RuntimeError("Missing /etc/hccn.conf")
    for path in ("/usr/local/Ascend/driver/version.info",
                 "/usr/local/Ascend/ascend-toolkit/latest/version.cfg"):
        if Path(path).is_file():
            print(path, Path(path).read_text())
    mark(directory, "DEVICE")
    torch.npu.set_device(args.device)
    mark(directory, "SETUP")
    store = MooncakeDistributedStore()
    # Seven positional arguments are shared by the installed SGLang integration
    # and the documented Store API. No tenant/version-specific kwargs assumed.
    result = store.setup(args.host, "P2PHANDSHAKE", 1 << 30, 1 << 30,
                         "ascend", "", f"127.0.0.1:{args.port}")
    print("setup returned:", result, flush=True)
    if result != 0:
        raise RuntimeError(f"Store setup failed: {result}")
    maps = Path("/proc/self/maps").read_text()
    (directory / "loaded-libraries.txt").write_text(maps)
    loaded = sorted({line.split()[-1] for line in maps.splitlines()
                     if any(word in line.lower() for word in ("hixl", "adxl", "ascend", "mooncake"))})
    print("Loaded runtime libraries:", json.dumps(loaded), flush=True)
    mark(directory, "PUTGET")
    prefix = "fabric-local-" + uuid.uuid4().hex
    for size in (4096, 65536, 1048576):
        key = f"{prefix}-{size}"
        payload = bytes(range(256)) * (size // 256)
        result = store.put(key, payload)
        print("put", size, "returned", result, flush=True)
        if result != 0:
            raise RuntimeError(f"put failed: {result}")
        received = store.get(key)
        if received != payload:
            raise RuntimeError(f"Data mismatch for {size} bytes")
        print("verified", size, "bytes", flush=True)
    if args.register_pinned:
        mark(directory, "PIN_ALLOC")
        # Match the NPU HiCache allocator: ordinary CPU pinned torch storage.
        tensor = torch.empty(args.pinned_mib * 1024 * 1024, dtype=torch.uint8,
                             device="cpu", pin_memory=True)
        print("pinned:", tensor.is_pinned(), "address:", tensor.data_ptr(),
              "bytes:", tensor.numel(), flush=True)
        mark(directory, "PIN_REGISTER")
        result = store.register_buffer(tensor.data_ptr(), tensor.numel())
        print("register_buffer returned:", result, flush=True)
        if result != 0:
            raise RuntimeError(f"Pinned registration failed: {result}")
        mark(directory, "PIN_OK")
        # Keep tensor alive until close has released all registrations.
    mark(directory, "CLOSE")
    store.close()
    mark(directory, "DONE")


def stop(process):
    if process is None:
        return
    # Only signal process groups created by this invocation.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main(registration=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=int, default=0, help="logical NPU ID (default 0)")
    parser.add_argument("--host", default="127.0.0.1", help="local address for single-node probe")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--register-pinned", action="store_true", default=registration,
                        help=argparse.SUPPRESS)
    parser.add_argument("--pinned-mib", type=int, default=1024,
                        help="pinned buffer MiB for registration probe (default 1024)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return 0
    if args.timeout < 1 or args.device < 0 or args.pinned_mib < 1:
        parser.error("timeout and pinned-mib must be positive; device must be nonnegative")
    prefix = "F3:" if args.register_pinned else "F1:"
    directory = Path(tempfile.mkdtemp(prefix="mooncake-fabric-local-"))
    print(f"Logs: {directory}", flush=True)
    print(f"Testing logical NPU {args.device}; Store=1 GiB, buffer=1 GiB; no network downloads.", flush=True)
    master = shutil.which("mooncake_master")
    if master is None:
        print(prefix + "MASTER_MISSING")
        return 1
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = os.environ.copy()
    env.update(ASCEND_ENABLE_USE_FABRIC_MEM="1", HCCL_INTRA_ROCE_ENABLE="0",
               ASCEND_GLOBAL_RESOURCE_CONFIG='{"fabric_memory.max_capacity":4}')
    # No reuse of an external master, tenant or data set. Store exits before master.
    processes = []
    try:
        with (directory / "master.log").open("w") as master_log, (directory / "probe.log").open("w") as probe_log:
            master_process = subprocess.Popen([master, f"--port={port}"],
                stdout=master_log, stderr=subprocess.STDOUT, cwd=directory,
                env=env, start_new_session=True)
            processes.append(master_process)
            deadline = time.monotonic() + 30
            while True:
                if master_process.poll() is not None:
                    raise RuntimeError("MASTER_EXIT")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("MASTER_TIMEOUT")
                    time.sleep(0.2)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                       "--output", str(directory), "--port", str(port),
                       "--device", str(args.device), "--host", args.host]
            if args.register_pinned:
                command.extend(["--register-pinned", "--pinned-mib", str(args.pinned_mib)])
            process = subprocess.Popen(command, stdout=probe_log, stderr=subprocess.STDOUT,
                                       env=env, start_new_session=True)
            processes.append(process)
            deadline = time.monotonic() + args.timeout
            previous = None
            while process.poll() is None:
                stage = (directory / "stage").read_text() if (directory / "stage").exists() else "START"
                if stage and stage != previous:
                    if not args.register_pinned:
                        print("Stage:", stage, flush=True)
                    previous = stage
                if time.monotonic() >= deadline:
                    raise RuntimeError("TIMEOUT_" + stage)
                time.sleep(0.5)
            stage = (directory / "stage").read_text() if (directory / "stage").exists() else "START"
            if process.returncode != 0 or stage != "DONE":
                raise RuntimeError(f"{stage}_EXIT{process.returncode}")
        if args.register_pinned:
            print("F3:BASE_OK,PIN_OK")
            return 0
        print("F1:LOCAL_OK")
        print("Ascend setup and local byte verification passed with Fabric mode requested.")
        print("Fabric allocation/transport evidence remains in probe.log; this does NOT prove remote HCCS.")
        return 0
    except (RuntimeError, OSError) as exc:
        print(prefix + str(exc), flush=True)
        (directory / "controller-error.log").write_text(traceback.format_exc())
        return 1
    finally:
        for process in reversed(processes):
            stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
