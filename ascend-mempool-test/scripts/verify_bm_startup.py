"""Check BM allocation, mappings and peer probes without allocating model KV."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from verify_graph import make_layout, parse_args, run

from ascend_mempool.control import TestChannel

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.manager import MempoolKVManager


def hold_local_pools(
    directory: Path, devices: list[int], device_id: int, timeout: float
) -> None:
    """Keep every local allocation alive until all selected devices have a pool."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"device-{device_id}.ready").touch(exist_ok=False)
    deadline = time.monotonic() + timeout
    while True:
        missing = [
            device
            for device in devices
            if not (directory / f"device-{device}.ready").exists()
        ]
        if not missing:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Local BM pools did not become ready: devices={missing}"
            )
        time.sleep(min(0.2, remaining))


def verify_probe_and_hold(manager: MempoolKVManager, args: argparse.Namespace) -> None:
    """Read back the peer's startup marker before announcing a live local pool."""
    expected = bytes([2 - args.rank]) * manager.layout.probe_bytes
    manager.verify_local_probe(expected, f"npu:{args.device_id}")
    print(
        f"[BM_STARTUP] PROBE_VERIFIED rank={args.rank} device={args.device_id} "
        f"bytes={len(expected)}",
        flush=True,
    )
    if args.local_ready_dir is not None:
        hold_local_pools(
            args.local_ready_dir, args.local_devices, args.device_id, args.timeout
        )
        print(
            f"[BM_STARTUP] LOCAL_POOLS_READY rank={args.rank} "
            f"device={args.device_id} devices={args.local_devices}",
            flush=True,
        )


def finish_probes(
    manager: MempoolKVManager,
    args: argparse.Namespace,
    torch: Any,
    channel: TestChannel,
) -> list[dict[str, Any]]:
    """Confirm both probes have drained before the existing paired pool teardown."""
    torch.npu.synchronize()
    channel.send("BM_STARTUP_DRAINED")
    channel.expect("BM_STARTUP_DRAINED")
    return [
        dict(
            check="bm_peer_probe",
            verified_bytes=manager.layout.probe_bytes,
            device_id=args.device_id,
            local_devices=args.local_devices,
        )
    ]


def main() -> int:
    """Reuse the graph gate's BM setup and teardown with only a 64-byte probe."""
    try:
        local_parser = argparse.ArgumentParser(add_help=False)
        local_parser.add_argument("--local-ready-dir", type=Path)
        local_parser.add_argument("--local-devices", type=int, nargs="+")
        local, remaining = local_parser.parse_known_args()
        args = parse_args(remaining)
        args.local_ready_dir = local.local_ready_dir
        args.local_devices = local.local_devices or [args.device_id]
        if (
            len(set(args.local_devices)) != len(args.local_devices)
            or any(device < 0 or device >= 16 for device in args.local_devices)
            or args.device_id not in args.local_devices
        ):
            raise ValueError(
                "local devices must be distinct IDs in [0, 16), including this device"
            )
        if len(args.local_devices) > 1 and args.local_ready_dir is None:
            raise ValueError("multiple local devices require a fresh --local-ready-dir")
        layout = make_layout(args)
        if args.describe:
            print(json.dumps(layout.signature(), indent=2))
            return 0
        logging.basicConfig(
            level=logging.INFO,
            format=f"%(asctime)s [BM_STARTUP rank={args.rank} device={args.device_id}] %(message)s",
        )
        print(
            f"[BM_STARTUP] START rank={args.rank} device={args.device_id} "
            f"pid={os.getpid()} local_dram_bytes={layout.contribution_bytes(args.rank)}",
            flush=True,
        )
        run(args, layout, stage=verify_probe_and_hold, paired_checks=finish_probes)
        if not args.check_env:
            print(
                f"[BM_STARTUP] PASSED rank={args.rank} device={args.device_id}",
                flush=True,
            )
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
