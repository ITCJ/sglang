#!/usr/bin/env python3
"""Prepare a small Mooncake Store dataset and keep the target worker alive.

Run this on the remote A3 node. The client connects to the master started here.
This is intentionally independent of SGLang and HiCache.
"""

import argparse
import os
import signal
import shutil
import socket
import subprocess
import time
from pathlib import Path

from kv_layout import PAGE_BYTES, PAGE_SIZE, page_keys, page_payload


def wait_port(host: str, port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("mooncake_master exited during startup")
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for master at {host}:{port}")


def main(argv=None, ready_code=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-ip", required=True, help="reachable IP of this node")
    parser.add_argument("--port", type=int, default=50071)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--tokens", type=int, default=1024)
    parser.add_argument("--segment-gib", type=int, default=1)
    parser.add_argument("--prefix", default="a3-kv-path-bench")
    parser.add_argument("--master-log", type=Path)
    args = parser.parse_args(argv)
    if args.tokens < PAGE_SIZE or args.tokens % PAGE_SIZE:
        parser.error("--tokens must be a positive multiple of 128")
    if args.segment_gib < 1 or args.device < 0:
        parser.error("--segment-gib must be positive and --device nonnegative")
    page_count = args.tokens // PAGE_SIZE
    if page_count * PAGE_BYTES >= args.segment_gib * (1 << 30):
        parser.error("Store segment is too small; increase --segment-gib")

    os.environ.setdefault("ASCEND_ENABLE_USE_FABRIC_MEM", "1")
    os.environ.setdefault("HCCL_INTRA_ROCE_ENABLE", "0")
    os.environ.setdefault("ASCEND_GLOBAL_RESOURCE_CONFIG", '{"fabric_memory.max_capacity":4}')

    import torch
    import torch_npu  # noqa: F401
    from mooncake.store import MooncakeDistributedStore

    torch.npu.set_device(args.device)
    master_bin = shutil.which("mooncake_master")
    if master_bin is None:
        raise RuntimeError("mooncake_master was not found in PATH")
    master_log = args.master_log.open("w") if args.master_log else None
    master = subprocess.Popen(
        [master_bin, f"--port={args.port}"],
        stdout=master_log if master_log is not None else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    store = None
    try:
        wait_port(args.local_ip, args.port, master)
        store = MooncakeDistributedStore()
        rc = store.setup(
            args.local_ip,
            "P2PHANDSHAKE",
            args.segment_gib * (1 << 30),
            1 << 30,
            "ascend",
            "",
            f"{args.local_ip}:{args.port}",
        )
        if rc != 0:
            raise RuntimeError(f"Mooncake setup returned {rc}")

        print(
            f"PREPARING prefix={args.prefix} pages={page_count} page_bytes={PAGE_BYTES}",
            flush=True,
        )
        for page, key in enumerate(page_keys(args.prefix, page_count)):
            rc = store.put(key, page_payload(page))
            if rc != 0:
                raise RuntimeError(f"put failed for {key}: {rc}")
        print(f"DATA_READY pages={page_count} bytes={page_count * PAGE_BYTES}", flush=True)
        if ready_code is not None:
            print(ready_code, flush=True)
        print("Leave this process running while the client benchmark executes.", flush=True)
        try:
            signal.pause()
        except KeyboardInterrupt:
            print("STORE_STOPPED", flush=True)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        if master.poll() is None:
            master.terminate()
            try:
                master.wait(timeout=5)
            except subprocess.TimeoutExpired:
                master.kill()
                master.wait()
        if master_log is not None:
            master_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
