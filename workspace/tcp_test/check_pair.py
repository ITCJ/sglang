#!/usr/bin/env python3
"""Two-node Store test; run target and client manually, without SSH.

Standalone TCP probe; no imports from the Fabric test directory.
Client contributes no segment. Writer exits before a fresh reader verifies data.
PASS proves remote TCP Store correctness, not RDMA/Fabric or HiCache integration.
"""
import argparse
import hashlib
import ipaddress
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
    temporary = directory / "stage.tmp"
    temporary.write_text(stage)
    temporary.replace(directory / "stage")
    print(stage, flush=True)


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def payload(token, size):
    block = hashlib.sha256((token + str(size)).encode()).digest()
    return (block * ((size + len(block) - 1) // len(block)))[:size]


def worker(args):
    directory = Path(args.output)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    mark(directory, "IMPORT")
    import importlib.metadata
    from mooncake.store import MooncakeDistributedStore

    for package in ("mooncake-transfer-engine-npu", "mooncake-transfer-engine"):
        try:
            print(package, importlib.metadata.version(package), flush=True)
        except importlib.metadata.PackageNotFoundError:
            pass
    print("setup API:", MooncakeDistributedStore.setup.__doc__, flush=True)
    tensor = None
    if args.pinned and args.mode == "client":
        mark(directory, "DEVICE")
        import torch
        import torch_npu  # noqa: F401
        torch.npu.set_device(args.device)
        mark(directory, "PIN_ALLOC")
        tensor = torch.empty(args.pinned_mib * 1024 * 1024, dtype=torch.uint8,
                             device="cpu", pin_memory=True)
        if not tensor.is_pinned():
            raise RuntimeError("tensor is not pinned")
        print("pinned bytes:", tensor.numel(), flush=True)
    mark(directory, "SETUP")
    store = MooncakeDistributedStore()
    segment = (256 << 20) if args.mode == "target" else 0
    print("protocol=tcp global_segment_size=", segment, flush=True)
    rc = store.setup(args.local_ip, "P2PHANDSHAKE", segment, 16 << 20,
                     "tcp", "", f"{args.target_ip}:{args.port}")
    if rc != 0:
        raise RuntimeError(f"setup returned {rc}")
    (directory / "loaded-libraries.txt").write_text(Path("/proc/self/maps").read_text())
    if tensor is not None:
        mark(directory, "PIN_REGISTER")
        rc = store.register_buffer(tensor.data_ptr(), tensor.numel())
        print("register_buffer returned:", rc, flush=True)
        if rc != 0:
            raise RuntimeError(f"register_buffer returned {rc}")
    if args.mode == "target":
        mark(directory, "READY")
        while not (directory / "stop").exists():
            time.sleep(0.5)
    else:
        mark(directory, args.worker.upper())
        for size in (4096, 65536, 1048576):
            key = f"tcp-pair-{args.token}-{size}"
            data = payload(args.token, size)
            if tensor is not None:
                import ctypes
                ptr = tensor.data_ptr()
                mark(directory, "PIN_" + args.worker.upper())
                if args.worker == "write":
                    ctypes.memmove(ptr, data, size)
                    results = list(store.batch_put_from([key], [ptr], [size]))
                    if results != [0]:
                        raise RuntimeError(f"batch_put_from({size}): {results}")
                else:
                    ctypes.memset(ptr, 0, size)
                    results = list(store.batch_get_into([key], [ptr], [size]))
                    if results != [size]:
                        raise RuntimeError(f"batch_get_into({size}): {results}")
                    if ctypes.string_at(ptr, size) != data:
                        raise RuntimeError(f"pinned read mismatch: {size}")
            elif args.worker == "write":
                rc = store.put(key, data)
                if rc != 0:
                    raise RuntimeError(f"put({size}) returned {rc}")
            elif store.get(key) != data:
                raise RuntimeError(f"read mismatch: {size}")
            print(args.worker, size, "sha256", hashlib.sha256(data).hexdigest(), flush=True)
    # Keep registered tensor alive until Store close completes.
    mark(directory, "CLOSE")
    store.close()
    mark(directory, "DONE")


def wait_port(ip, port, process=None, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise RuntimeError("MASTER_EXIT")
        try:
            with socket.create_connection((ip, port), timeout=1):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("MASTER_UNREACHABLE")
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("target", "client"))
    parser.add_argument("--local-ip", required=True, help="this node's reachable IPv4")
    parser.add_argument("--target-ip", help="target node IPv4; required for client")
    parser.add_argument("--port", type=int, default=50081)
    parser.add_argument("--pinned", action="store_true",
                        help="client: test pinned registration and batch pointer IO")
    parser.add_argument("--pinned-mib", type=int, default=1024,
                        help="client pinned allocation MiB (default 1024)")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=180, help="per-worker timeout")
    parser.add_argument("--worker", choices=("serve", "write", "read"), help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mode == "target":
        args.target_ip = args.local_ip
    if not args.target_ip:
        parser.error("client requires --target-ip")
    try:
        for value in (args.local_ip, args.target_ip):
            ip = ipaddress.IPv4Address(value)
            if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                raise ValueError("use reachable unicast node IPs")
        if args.mode == "client" and args.local_ip == args.target_ip:
            raise ValueError("client and target must have different IPs")
        if args.pinned_mib < 1 or args.device < 0 or args.timeout < 1 or not 1024 <= args.port <= 65535:
            raise ValueError("invalid device, timeout or port")
    except ValueError as exc:
        parser.error(str(exc))
    if args.worker:
        worker(args)
        return 0
    # Confirm the supplied local address belongs to this network namespace.
    try:
        with socket.socket() as check:
            check.bind((args.local_ip, 0))
        if args.mode == "client":
            with socket.socket() as check:
                try:
                    check.bind((args.target_ip, 0))
                except OSError:
                    pass
                else:
                    raise RuntimeError("TARGET_IS_LOCAL")
    except (OSError, RuntimeError):
        print("TCP:CHECK_IPS")
        return 1
    directory = Path(tempfile.mkdtemp(prefix="mooncake-tcp-pair-"))
    print("Logs:", directory, flush=True)
    print("TCP; 16 MiB buffer; target alone contributes 256 MiB Store.", flush=True)
    env = os.environ.copy()
    # Only child environments change; never activate the Ascend transport.
    for key in ("ASCEND_ENABLE_USE_FABRIC_MEM", "HCCL_INTRA_ROCE_ENABLE",
                "ASCEND_GLOBAL_RESOURCE_CONFIG", "MC_MS_AUTO_DISC"):
        env.pop(key, None)
    env["MOONCAKE_PROTOCOL"] = "tcp"
    env["PYTHONUNBUFFERED"] = "1"
    processes, handles = [], []
    args.token = uuid.uuid4().hex

    def launch(phase):
        folder = directory / phase
        folder.mkdir()
        handle = (folder / "probe.log").open("w")
        handles.append(handle)
        command = [sys.executable, str(Path(__file__).resolve()), args.mode,
                   "--local-ip", args.local_ip, "--target-ip", args.target_ip,
                   "--port", str(args.port), "--device", str(args.device),
                   "--worker", phase, "--output", str(folder), "--token", args.token]
        if args.pinned:
            command += ["--pinned", "--pinned-mib", str(args.pinned_mib)]
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        processes.append(process)
        deadline, previous = time.monotonic() + args.timeout, None
        while True:
            stage = (folder / "stage").read_text() if (folder / "stage").exists() else "START"
            rc = process.poll()
            if stage and stage != previous:
                print(phase + ":", stage, flush=True)
                previous = stage
            if rc is not None:
                if phase == "serve" or rc != 0 or stage != "DONE":
                    raise RuntimeError(f"{phase}_{stage}_EXIT{rc}")
                return process
            if phase == "serve" and stage == "READY":
                return process
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{phase}_{stage}_TIMEOUT")
            time.sleep(0.5)

    try:
        if args.mode == "target":
            master = shutil.which("mooncake_master")
            if not master:
                raise RuntimeError("MASTER_MISSING")
            with socket.socket() as check:
                check.bind(("0.0.0.0", args.port))
            handle = (directory / "master.log").open("w")
            handles.append(handle)
            master_process = subprocess.Popen([master, f"--port={args.port}"],
                stdout=handle, stderr=subprocess.STDOUT, env=env,
                cwd=directory, start_new_session=True)
            processes.append(master_process)
            wait_port(args.local_ip, args.port, master_process)
            serving = launch("serve")
            print("TCP:READY — leave this terminal running; Ctrl+C after client finishes.", flush=True)
            while serving.poll() is None and master_process.poll() is None:
                time.sleep(1)
            raise RuntimeError("TARGET_EXIT")
        wait_port(args.target_ip, args.port)
        launch("write")
        launch("read")
        print("TCP:PINNED_REMOTE_OK" if args.pinned else "TCP:REMOTE_OK")
        print("Fresh-process remote TCP read verified; no RDMA/Fabric or HiCache performance claim.")
        return 0
    except KeyboardInterrupt:
        print("TCP:STOPPED")
        return 130
    except (RuntimeError, OSError) as exc:
        print("TCP:" + str(exc))
        (directory / "controller-error.log").write_text(traceback.format_exc())
        return 1
    finally:
        if (directory / "serve").exists():
            (directory / "serve" / "stop").touch()
            if processes:
                try:
                    processes[-1].wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        for process in reversed(processes):
            stop(process)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
