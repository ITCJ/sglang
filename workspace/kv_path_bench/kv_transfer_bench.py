#!/usr/bin/env python3
"""Compare three ways to materialize remote MLA pages into Ascend L1 KV."""

import argparse
import ctypes
import json
import math
import os
import statistics
import time
from pathlib import Path

from kv_layout import (
    K_DIM,
    LAYERS,
    PAGE_BYTES,
    PAGE_SIZE,
    ROPE_DIM,
    page_keys,
    page_payload,
)


WARMUP = 2
REPEATS = 10
GIB = 1 << 30


def split_page(packed, host_k, host_rope, page: int) -> None:
    host_k[page + 1].copy_(packed[..., :K_DIM])
    host_rope[page + 1].copy_(packed[..., K_DIM:])


def check_result(device_k, device_rope, count: int, torch) -> None:
    k = device_k[:, 1:].cpu()
    rope = device_rope[:, 1:].cpu()
    for page in range(count):
        expected = torch.frombuffer(bytearray(page_payload(page)), dtype=torch.bfloat16)
        expected = expected.view(LAYERS, PAGE_SIZE, 1, K_DIM + ROPE_DIM)
        # Compare bytes: arbitrary BF16 payloads need not compare equal as floats.
        for actual, reference in (
            (k[:, page], expected[..., :K_DIM]),
            (rope[:, page], expected[..., K_DIM:]),
        ):
            if not torch.equal(
                actual.contiguous().view(torch.uint8),
                reference.contiguous().view(torch.uint8),
            ):
                raise RuntimeError(f"NPU KV mismatch at page {page}")


def read_pages(store, keys, page_ptrs) -> None:
    result = store.batch_get_into(keys, page_ptrs, [PAGE_BYTES] * len(keys))
    if len(result) != len(keys) or any(size != PAGE_BYTES for size in result):
        raise RuntimeError(f"Mooncake short/failed read: {result}")


def summarize(
    path: str, samples: list[float], nbytes: int, host_extra=0, npu_extra=0
) -> dict:
    ordered = sorted(samples)
    median = statistics.median(ordered)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    return {
        "path": path,
        "median_s": median,
        "p95_s": p95,
        "effective_gbps": nbytes / median / 1e9,
        "host_staging_bytes": host_extra,
        "npu_staging_bytes": npu_extra,
        "samples_s": samples,
        "correct": True,
    }


def measure(action, device_k, device_rope, torch) -> list[float]:
    samples = []
    for iteration in range(WARMUP + REPEATS):
        device_k.zero_()
        device_rope.zero_()
        torch.npu.synchronize()
        start = time.perf_counter()
        action()
        torch.npu.synchronize()
        if iteration >= WARMUP:
            samples.append(time.perf_counter() - start)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-ip", required=True)
    parser.add_argument("--master-ip", required=True)
    parser.add_argument("--port", type=int, default=50071)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--tokens", type=int, default=1024)
    parser.add_argument("--prefix", default="a3-kv-path-bench")
    parser.add_argument("--output", type=Path, default=Path("kv-transfer-results.json"))
    parser.add_argument("--skip-direct", action="store_true", help="only measure L2 and L3 via L2")
    args = parser.parse_args()
    if args.tokens < PAGE_SIZE or args.tokens % PAGE_SIZE:
        parser.error("--tokens must be a positive multiple of 128")
    if args.device < 0:
        parser.error("--device must be nonnegative")

    os.environ.setdefault("ASCEND_ENABLE_USE_FABRIC_MEM", "1")
    os.environ.setdefault("HCCL_INTRA_ROCE_ENABLE", "0")
    os.environ.setdefault("ASCEND_GLOBAL_RESOURCE_CONFIG", '{"fabric_memory.max_capacity":4}')

    import torch
    import torch_npu  # noqa: F401
    from mooncake.store import MooncakeDistributedStore, MooncakeHostMemAllocator
    from sgl_kernel_npu.kvcacheio import TransferDirection, transfer_kv_dim_exchange

    torch.npu.set_device(args.device)
    count = args.tokens // PAGE_SIZE
    page_num = count + 1  # reserve page 0 as in NPUMLATokenToKVPool
    host_shape = (page_num, LAYERS, PAGE_SIZE, 1)
    device_shape = (LAYERS, page_num, PAGE_SIZE, 1)
    host_k = torch.empty((*host_shape, K_DIM), dtype=torch.bfloat16, pin_memory=True)
    host_rope = torch.empty((*host_shape, ROPE_DIM), dtype=torch.bfloat16, pin_memory=True)
    device_k = torch.zeros((*device_shape, K_DIM), dtype=torch.bfloat16, device="npu")
    device_rope = torch.zeros((*device_shape, ROPE_DIM), dtype=torch.bfloat16, device="npu")
    indices = torch.arange(PAGE_SIZE, page_num * PAGE_SIZE, dtype=torch.int64)
    keys = page_keys(args.prefix, count)
    total_bytes = count * PAGE_BYTES

    def load_l1():
        transfer_kv_dim_exchange(
            device_indices=indices,
            host_indices=indices,
            device_k=device_k,
            host_k=host_k,
            device_v=device_rope,
            host_v=host_rope,
            device_index_k=None,
            host_index_k=None,
            page_size=PAGE_SIZE,
            direction=TransferDirection.H2D,
        )

    result = {
        "tokens": args.tokens,
        "pages": count,
        "page_bytes": PAGE_BYTES,
        "bytes": total_bytes,
        "warmup": WARMUP,
        "repeats": REPEATS,
        "object_layout": "[layer,token,compressed_kv_512,rope_64]",
        "paths": [],
    }
    store = MooncakeDistributedStore()
    try:
        rc = store.setup(args.local_ip, "P2PHANDSHAKE", 0, GIB, "ascend", "", f"{args.master_ip}:{args.port}")
        if rc != 0:
            raise RuntimeError(f"Mooncake setup returned {rc}")

        for page in range(count):
            packed = torch.frombuffer(bytearray(page_payload(page)), dtype=torch.bfloat16)
            split_page(packed.view(LAYERS, PAGE_SIZE, 1, K_DIM + ROPE_DIM), host_k, host_rope, page)
        samples = measure(load_l1, device_k, device_rope, torch)
        check_result(device_k, device_rope, count, torch)
        result["paths"].append(summarize("L2->L1", samples, total_bytes))

        # The only Mooncake-registered Host buffer is a whole-page receive area.
        # L2 stays in the split, pinned layout used by Ascend HiCache.
        allocator = MooncakeHostMemAllocator()
        ptr = allocator.alloc(total_bytes)
        if not ptr:
            raise RuntimeError(f"MooncakeHostMemAllocator.alloc({total_bytes}) failed")
        backing = (ctypes.c_byte * total_bytes).from_address(int(ptr))
        staging = torch.frombuffer(backing, dtype=torch.bfloat16)
        staging = staging.view(count, LAYERS, PAGE_SIZE, 1, K_DIM + ROPE_DIM)
        rc = store.register_buffer(int(ptr), total_bytes)
        if rc != 0:
            raise RuntimeError(f"Mooncake Host buffer registration failed: {rc}")
        stage_ptrs = [staging[page].data_ptr() for page in range(count)]

        def via_l2():
            read_pages(store, keys, stage_ptrs)
            for page in range(count):
                split_page(staging[page], host_k, host_rope, page)
            load_l1()

        samples = measure(via_l2, device_k, device_rope, torch)
        check_result(device_k, device_rope, count, torch)
        result["paths"].append(
            summarize("L3->L2->L1", samples, total_bytes, total_bytes)
        )

        if not args.skip_direct:
            # Store writes page objects into NPU staging; reshape to the exact
            # same layer-first L1 destinations, with the reshape timed.
            direct = torch.empty(
                (count, LAYERS, PAGE_SIZE, 1, K_DIM + ROPE_DIM),
                dtype=torch.bfloat16,
                device="npu",
            )
            try:
                rc = store.register_buffer(direct.data_ptr(), total_bytes)
            except Exception as exc:
                result["paths"].append(
                    {"path": "L3->NPU staging->L1", "status": "unavailable", "error": repr(exc)}
                )
            else:
                if rc != 0:
                    result["paths"].append(
                        {
                            "path": "L3->NPU staging->L1",
                            "status": "unavailable",
                            "error": f"NPU register_buffer returned {rc}",
                        }
                    )
                else:
                    direct_ptrs = [direct[page].data_ptr() for page in range(count)]

                    def direct_to_l1():
                        read_pages(store, keys, direct_ptrs)
                        device_k[:, 1:].copy_(direct[..., :K_DIM].permute(1, 0, 2, 3, 4))
                        device_rope[:, 1:].copy_(direct[..., K_DIM:].permute(1, 0, 2, 3, 4))

                    samples = measure(direct_to_l1, device_k, device_rope, torch)
                    check_result(device_k, device_rope, count, torch)
                    result["paths"].append(
                        summarize(
                            "L3->NPU staging->L1",
                            samples,
                            total_bytes,
                            npu_extra=total_bytes,
                        )
                    )

        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
