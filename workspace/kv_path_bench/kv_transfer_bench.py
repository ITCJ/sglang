#!/usr/bin/env python3
"""Measure L2->L1 and L3->L2->L1 with and without extra Host staging."""

import argparse
import ctypes
import json
import math
import os
import statistics
import subprocess
import time
import traceback
from pathlib import Path

from bench_ports import MASTER_PORT
from feasibility_check import check_page
from feasibility_log import enable_log, print_result
from kv_layout import K_DIM, LAYERS, PAGE_BYTES, PAGE_SIZE, ROPE_DIM, page_keys, split_page_payload


GIB = 1 << 30
BATCH_PAGES = 8
WARMUP = 2
REPEATS = 10
PATHS = {
    "A": "L2->L1",
    "B": "L3->Host staging->L2->L1",
    "C": "L3->L2->L1",
}


def make_batches(count: int, batch_pages: int = BATCH_PAGES, layout: str = "scattered") -> list[dict]:
    """Same bounded batches and non-overlapping L1 destinations for every path."""
    stride = 137 if layout == "scattered" else 1
    while math.gcd(stride, count) != 1:
        stride += 1
    return [
        {
            "pages": list(range(start, min(start + batch_pages, count))),
            "slots": [1 + (page * stride) % count for page in range(start, min(start + batch_pages, count))],
        }
        for start in range(0, count, batch_pages)
    ]


def measure_batch(action, prepare, synchronize, warmup, repeats, clock=time.perf_counter):
    """Exclude input/reset preparation; include completion of each reusable batch."""
    samples = []
    for iteration in range(warmup + repeats):
        prepare()
        synchronize()
        start = clock()
        action()
        synchronize()
        elapsed = clock() - start
        if iteration >= warmup:
            samples.append(elapsed)
    return samples


def summarize(code: str, batch_samples: list[list[float]], nbytes: int, staging_bytes: int) -> dict:
    # Sample r is the sum of timed batch r measurements, not a continuous sweep.
    samples = [sum(values) for values in zip(*batch_samples)]
    median = statistics.median(samples)
    return {
        "code": code,
        "path": PATHS[code],
        "median_s": median,
        "p95_s": sorted(samples)[math.ceil(0.95 * len(samples)) - 1],
        "effective_gbps": nbytes / median / 1e9,
        "host_staging_bytes": staging_bytes,
        "samples_s": samples,
        "batch_samples_s": batch_samples,
        "correct": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_ip", nargs="?")
    parser.add_argument("store_ip", nargs="?")
    parser.add_argument("size", nargs="?", choices=("small", "max"))
    parser.add_argument("--local-ip", dest="local_ip")
    parser.add_argument("--master-ip", dest="master_ip")
    parser.add_argument("--tokens", type=int)
    parser.add_argument("--port", type=int, default=MASTER_PORT)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--prefix", default="a3-kv-path-bench")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--layout", choices=("contiguous", "scattered"), default="scattered")
    parser.add_argument("--warmup", type=int, default=WARMUP)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--skip-direct", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    local_ip, master_ip = args.local_ip or args.client_ip, args.master_ip or args.store_ip
    if not local_ip or not master_ip:
        parser.error("provide client and Store IPs")
    tokens = args.tokens if args.tokens is not None else {"small": 128, "max": 131072, None: 1024}[args.size]
    if args.size and args.tokens is not None and tokens != {"small": 128, "max": 131072}[args.size]:
        parser.error("size and --tokens disagree")
    if not PAGE_SIZE <= tokens <= 131072 or tokens % PAGE_SIZE:
        parser.error("--tokens must be a multiple of 128 between 128 and 131072")
    if args.device < 0 or args.warmup < 0 or args.repeats < 1:
        parser.error("device/warmup must be nonnegative; repeats must be positive")

    output = args.output or Path(f"/tmp/a3-kv-perf-{tokens}.json")
    enable_log("perf", str(tokens), path=args.log or Path(f"/tmp/a3-kv-perf-{tokens}.log"))
    os.environ.setdefault("ASCEND_ENABLE_USE_FABRIC_MEM", "1")
    os.environ.setdefault("HCCL_INTRA_ROCE_ENABLE", "0")
    os.environ.setdefault("ASCEND_GLOBAL_RESOURCE_CONFIG", '{"fabric_memory.max_capacity":4}')

    count = tokens // PAGE_SIZE
    batch = min(count, BATCH_PAGES)
    k_page_elements = LAYERS * PAGE_SIZE * K_DIM
    k_page_bytes = k_page_elements * 2
    rope_page_bytes = PAGE_BYTES - k_page_bytes
    host_shape = (batch + 1, LAYERS, PAGE_SIZE, 1)
    l2_bytes = (batch + 1) * PAGE_BYTES
    staging_bytes = batch * PAGE_BYTES
    keys = page_keys(args.prefix + "-split", count)
    batches = make_batches(count, layout=args.layout)
    result = {
        "status": "running", "tokens": tokens, "pages": count, "page_bytes": PAGE_BYTES,
        "layout": args.layout,
        "bytes": count * PAGE_BYTES, "batch_pages": batch,
        "warmup": args.warmup, "repeats": args.repeats,
        "object_layout": "one key per page: all compressed KV, then all RoPE",
        "l2_layout": "page,layer,token,1,dim; separate KV/RoPE in ADXL Host buffer",
        "l1_layout": "layer,page,token,1,dim; separate KV/RoPE",
        "l1_slots": [slot for item in batches for slot in item["slots"]],
        "timing": "sum of synchronized per-batch wall times; each batch warmed independently",
        "path_order": "rotate A/B/C per batch",
        "l2_bytes": l2_bytes, "allocated_host_staging_bytes": staging_bytes,
        "store_local_buffer_bytes": GIB,
        "l1_bytes": (count + 1) * PAGE_BYTES,
        "disabled_paths": ["L3->NPU staging->L1"], "paths": [],
        "commit": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
            capture_output=True, text=True,
        ).stdout.strip(),
    }
    store = pool = None
    leases = []
    stage = "imports"
    failure = "F9"
    try:
        import torch
        import torch_npu  # noqa: F401
        from mooncake.store import BufferPool, MooncakeDistributedStore
        from sgl_kernel_npu.kvcacheio import TransferDirection, transfer_kv_dim_exchange

        failure, stage = "F2", "L1 allocation and Store setup"
        torch.npu.set_device(args.device)
        device_k = torch.empty((LAYERS, count + 1, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        device_rope = torch.empty((LAYERS, count + 1, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        store = MooncakeDistributedStore()
        rc = store.setup(local_ip, "P2PHANDSHAKE", 0, GIB, "ascend", "", f"{master_ip}:{args.port}")
        if rc != 0:
            raise RuntimeError(f"Store setup returned {rc}")

        failure, stage = "F3", "ADXL L2 and staging allocation"
        pool = BufferPool(store, block_on_exhaustion=False)

        def host_tensor(nbytes):
            lease = pool.acquire(nbytes)
            leases.append(lease)
            backing = (ctypes.c_byte * nbytes).from_address(int(lease.ptr))
            return torch.frombuffer(backing, dtype=torch.bfloat16)

        l2 = host_tensor(l2_bytes)
        k_elements = (batch + 1) * k_page_elements
        host_k = l2[:k_elements].view(*host_shape, K_DIM)
        host_rope = l2[k_elements:].view(*host_shape, ROPE_DIM)
        staging = host_tensor(staging_bytes).view(batch, PAGE_BYTES // 2)
        batch_samples = {code: [] for code in PATHS}
        for batch_index, item in enumerate(batches):
            pages, slots = item["pages"], item["slots"]
            n = len(pages)
            selected_keys = [keys[page] for page in pages]
            host_indices = torch.arange(PAGE_SIZE, (n + 1) * PAGE_SIZE, dtype=torch.int64)
            device_indices = torch.tensor(
                [slot * PAGE_SIZE + token for slot in slots for token in range(PAGE_SIZE)],
                dtype=torch.int64,
            )
            staged_ptrs = [staging[i].data_ptr() for i in range(n)]
            target_ptrs = [[host_k[i + 1].data_ptr(), host_rope[i + 1].data_ptr()] for i in range(n)]
            target_sizes = [[k_page_bytes, rope_page_bytes] for _ in range(n)]
            # At most eight pages of expected Host data, created outside timing.
            expected = [torch.frombuffer(bytearray(split_page_payload(page)), dtype=torch.bfloat16) for page in pages]

            def load_l1():
                transfer_kv_dim_exchange(
                    device_indices=device_indices, host_indices=host_indices,
                    device_k=device_k, host_k=host_k, device_v=device_rope, host_v=host_rope,
                    device_index_k=None, host_index_k=None, page_size=PAGE_SIZE,
                    direction=TransferDirection.H2D,
                )

            def run_path(code):
                if code == "B":
                    rc = list(store.batch_get_into(selected_keys, staged_ptrs, [PAGE_BYTES] * n))
                    if rc != [PAGE_BYTES] * n:
                        raise RuntimeError(f"staged read failed: {rc}")
                    for i in range(n):
                        host_k[i + 1].copy_(staging[i, :k_page_elements].view_as(host_k[i + 1]))
                        host_rope[i + 1].copy_(staging[i, k_page_elements:].view_as(host_rope[i + 1]))
                elif code == "C":
                    rc = list(store.batch_get_into_multi_buffers(selected_keys, target_ptrs, target_sizes))
                    if rc != [PAGE_BYTES] * n:
                        raise RuntimeError(f"direct L2 read failed: {rc}")
                load_l1()

            order = list(PATHS)
            offset = batch_index % len(order)
            for code in order[offset:] + order[:offset]:
                failure, stage = "F5", f"performance {code} batch={batch_index}"
                if code == "A":
                    for i, packed in enumerate(expected):
                        host_k[i + 1].copy_(packed[:k_page_elements].view_as(host_k[i + 1]))
                        host_rope[i + 1].copy_(packed[k_page_elements:].view_as(host_rope[i + 1]))

                def prepare():
                    for slot in slots:
                        device_k[:, slot].zero_()
                        device_rope[:, slot].zero_()
                    if code != "A":
                        host_k.zero_()
                        host_rope.zero_()
                    if code == "B":
                        staging.zero_()

                samples = measure_batch(
                    lambda: run_path(code), prepare, torch.npu.synchronize,
                    args.warmup, args.repeats,
                )
                # Validate every logical page for every path, outside timed regions.
                stage = f"validation {code} batch={batch_index}"
                for page, slot in zip(pages, slots):
                    check_page(device_k, device_rope, slot, page, torch)
                batch_samples[code].append(samples)
            print(f"BATCH_OK pages={pages[0]}-{pages[-1]}", flush=True)

        result["paths"] = [
            summarize(code, batch_samples[code], count * PAGE_BYTES, staging_bytes if code == "B" else 0)
            for code in PATHS
        ]
        result["status"] = "ok"
    except Exception as exc:
        result.update(status="failed", stage=stage, error=repr(exc))
        print(f"PERFORMANCE_FAIL stage={stage} error={exc!r}", flush=True)
        traceback.print_exc()
    finally:
        try:
            for lease in reversed(leases):
                lease.release()
            if pool is not None:
                pool.close()
        finally:
            if store is not None:
                store.close()
        output.write_text(json.dumps(result, indent=2) + "\n")
    if result["status"] != "ok":
        print(failure, flush=True)
        return 1
    summary = " ".join(f"{item['code']}={item['median_s'] * 1000:.3f}" for item in result["paths"])
    suffix = "C" if args.layout == "contiguous" else "S"
    print_result(f"T{tokens}{suffix} {summary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        print("F9", flush=True)
        raise SystemExit(1)
