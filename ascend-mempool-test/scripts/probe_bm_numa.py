"""Diagnose local 1 GiB BM allocation with independent device and NUMA IDs.

Run one case per process. This deliberately stops before join/peer mapping and
does not test data transfer, model loading, or concurrent pool allocation.
"""

from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import os
import time
import traceback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", type=int, required=True)
    parser.add_argument("--numa-node", type=int, required=True, help="-1: SDK default")
    parser.add_argument("--host-ip", required=True, help="Local IPv4 address")
    parser.add_argument("--store-port", type=int, default=23300)
    parser.add_argument("--nic-port", type=int, default=23301)
    parser.add_argument(
        "--describe", action="store_true", help="No NPU imports/allocation"
    )
    args = parser.parse_args()
    if args.device_id < 0 or not -1 <= args.numa_node <= 126:
        parser.error("device-id must be nonnegative; numa-node must be in [-1, 126]")
    if (
        not 1 <= args.store_port <= 65535
        or not 1 <= args.nic_port <= 65535
        or args.store_port == args.nic_port
    ):
        parser.error("store/nic ports must be distinct and in [1, 65535]")
    try:
        ipaddress.IPv4Address(args.host_ip)
    except ipaddress.AddressValueError:
        parser.error("host-ip must be a local IPv4 address")
    flags = 0 if args.numa_node == -1 else 0x80 | args.numa_node
    result = dict(
        device_id=args.device_id,
        numa_node=args.numa_node,
        flags=flags,
        local_dram_bytes=1 << 30,
        world_size=1,
        store=f"tcp://{args.host_ip}:{args.store_port}",
        nic=f"tcp://{args.host_ip}:{args.nic_port}",
    )
    if args.describe:
        print(json.dumps(result, sort_keys=True))
        return 0

    print("[BM_NUMA] START " + json.dumps(result, sort_keys=True), flush=True)
    cleanup = []
    stage = "import"
    started = time.monotonic()
    result.update(pid=os.getpid(), allocation_ok=False, cleanup_errors=[])
    try:
        torch = importlib.import_module("torch")
        importlib.import_module("torch_npu")
        stage = "npu.set_device"
        torch.npu.set_device(args.device_id)
        mf = importlib.import_module("memfabric_hybrid")
        result["mf_module"] = mf.__file__
        mf.set_log_level(1)
        stage = "mf.initialize"
        ret = mf.initialize()
        if ret != 0:
            raise RuntimeError(f"MF initialize returned {ret}")
        cleanup.append(("mf.uninitialize", mf.uninitialize))
        config = mf.bm.BmConfig()
        config.auto_ranking = False
        config.rank_id = 0
        config.start_store = True
        config.init_timeout = config.create_timeout = config.operation_timeout = 30
        config.set_nic(result["nic"])
        stage = "bm.initialize"
        ret = mf.bm.initialize(result["store"], 1, args.device_id, config)
        if ret != 0:
            raise RuntimeError(f"BM initialize returned {ret}")
        cleanup.append(("bm.uninitialize", mf.bm.uninitialize))
        stage = "bm.create2"
        handle = mf.bm.create2(
            id=105,
            local_dram_size=1 << 30,
            max_dram_size=1 << 30,
            local_hbm_size=0,
            max_hbm_size=0,
            data_op_type=mf.bm.BmDataOpType.SDMA,
            flags=flags,
        )
        if handle is None:
            raise RuntimeError("BM create2 returned no handle")
        cleanup.append(("bm.destroy", handle.destroy))
        stage = "bm.local_mem_size"
        actual = handle.local_mem_size(mf.bm.BmMemType.HOST)
        if actual != 1 << 30:
            raise RuntimeError(f"Expected 1 GiB local allocation, received {actual}")
        result["allocation_ok"] = True
        print("[BM_NUMA] ALLOCATED " + json.dumps(result, sort_keys=True), flush=True)
    except Exception as exc:
        result.update(failed_stage=stage, error=repr(exc))
        traceback.print_exc()
    finally:
        for name, release in reversed(cleanup):
            try:
                ret = release()
                if ret not in (None, 0):
                    raise RuntimeError(f"cleanup returned {ret}")
            except Exception as exc:
                result["cleanup_errors"].append(f"{name}: {exc!r}")
                traceback.print_exc()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print("[BM_NUMA] RESULT " + json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["allocation_ok"] and not result["cleanup_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
