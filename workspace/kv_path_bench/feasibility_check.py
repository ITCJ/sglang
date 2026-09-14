#!/usr/bin/env python3
"""Check both remote MLA paths once, without timing or repeat sampling."""

import argparse
import ctypes
import os
import traceback

from bench_ports import MASTER_PORT
from feasibility_log import enable_log
from kv_layout import (
    K_DIM,
    LAYERS,
    PAGE_BYTES,
    PAGE_SIZE,
    ROPE_DIM,
    page_keys,
    page_payload,
)


GIB = 1 << 30
BATCH_PAGES = 8


def failure_code(stage: str) -> str:
    if stage in ("L1/Host allocation", "store setup"):
        return "F2"
    if stage in ("staging allocation", "Host staging registration"):
        return "F3"
    if stage == "NPU staging registration":
        return "F4"
    if stage.startswith("L3_L2_L1"):
        return "F5"
    if stage.startswith("L3_NPU_L1"):
        return "F6"
    return "F9"


def destination(page: int, count: int, scattered: bool) -> int:
    return 1 + ((page * 137) % count if scattered else page)


def check_page(device_k, device_rope, slot: int, page: int, torch) -> None:
    expected = torch.frombuffer(bytearray(page_payload(page)), dtype=torch.bfloat16)
    expected = expected.view(LAYERS, PAGE_SIZE, 1, K_DIM + ROPE_DIM)
    for actual, reference in (
        (device_k[:, slot].cpu(), expected[..., :K_DIM]),
        (device_rope[:, slot].cpu(), expected[..., K_DIM:]),
    ):
        if not torch.equal(
            actual.contiguous().view(torch.uint8),
            reference.contiguous().view(torch.uint8),
        ):
            raise RuntimeError(f"KV mismatch for Store page {page} at L1 slot {slot}")


def read_pages(store, keys, ptrs) -> None:
    result = list(store.batch_get_into(keys, ptrs, [PAGE_BYTES] * len(keys)))
    if result != [PAGE_BYTES] * len(keys):
        raise RuntimeError(f"Mooncake short/failed read: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_ip")
    parser.add_argument("store_ip")
    parser.add_argument("size", choices=("small", "max"))
    parser.add_argument("--device", type=int, default=0)
    args = parser.parse_args()
    if args.device < 0:
        parser.error("--device must be nonnegative")
    enable_log("client", args.size)

    os.environ.setdefault("ASCEND_ENABLE_USE_FABRIC_MEM", "1")
    os.environ.setdefault("HCCL_INTRA_ROCE_ENABLE", "0")
    os.environ.setdefault("ASCEND_GLOBAL_RESOURCE_CONFIG", '{"fabric_memory.max_capacity":4}')

    import torch
    import torch_npu  # noqa: F401
    from mooncake.store import MooncakeDistributedStore, MooncakeHostMemAllocator
    from sgl_kernel_npu.kvcacheio import TransferDirection, transfer_kv_dim_exchange

    torch.npu.set_device(args.device)
    count = 1 if args.size == "small" else 1024
    scattered = args.size == "max"
    keys = page_keys("a3-kv-path-bench", count)
    batch = min(count, BATCH_PAGES)
    host_shape = (batch + 1, LAYERS, PAGE_SIZE, 1)
    device_shape = (LAYERS, count + 1, PAGE_SIZE, 1)
    stage_bytes = batch * PAGE_BYTES

    store = MooncakeDistributedStore()
    stage = "L1/Host allocation"
    try:
        host_k = torch.empty((*host_shape, K_DIM), dtype=torch.bfloat16, pin_memory=True)
        host_rope = torch.empty((*host_shape, ROPE_DIM), dtype=torch.bfloat16, pin_memory=True)
        device_k = torch.empty((*device_shape, K_DIM), dtype=torch.bfloat16, device="npu")
        device_rope = torch.empty((*device_shape, ROPE_DIM), dtype=torch.bfloat16, device="npu")

        stage = "store setup"
        rc = store.setup(
            args.client_ip, "P2PHANDSHAKE", 0, GIB, "ascend", "", f"{args.store_ip}:{MASTER_PORT}"
        )
        if rc != 0:
            raise RuntimeError(f"Mooncake setup returned {rc}")

        stage = "staging allocation"
        allocator = MooncakeHostMemAllocator()
        ptr = allocator.alloc(stage_bytes)
        if not ptr:
            raise RuntimeError(f"MooncakeHostMemAllocator.alloc({stage_bytes}) failed")
        backing = (ctypes.c_byte * stage_bytes).from_address(int(ptr))
        staging = torch.frombuffer(backing, dtype=torch.bfloat16)
        staging = staging.view(batch, LAYERS, PAGE_SIZE, 1, K_DIM + ROPE_DIM)
        direct = torch.empty(staging.shape, dtype=torch.bfloat16, device="npu")

        stage = "Host staging registration"
        rc = store.register_buffer(int(ptr), stage_bytes)
        if rc != 0:
            raise RuntimeError(f"register_buffer returned {rc}")
        stage = "NPU staging registration"
        rc = store.register_buffer(direct.data_ptr(), stage_bytes)
        if rc != 0:
            raise RuntimeError(f"register_buffer returned {rc}")

        print(
            f"CHECK_BEGIN size={args.size} pages={count} "
            f"layout={'scattered' if scattered else 'contiguous'}",
            flush=True,
        )
        for path in ("L3_L2_L1", "L3_NPU_L1"):
            for start in range(0, count, batch):
                n = min(batch, count - start)
                stage = f"{path} pages={start}-{start + n - 1}"
                pages = range(start, start + n)
                slots = [destination(page, count, scattered) for page in pages]
                if path == "L3_L2_L1":
                    read_pages(
                        store,
                        keys[start : start + n],
                        [staging[i].data_ptr() for i in range(n)],
                    )
                    for i in range(n):
                        host_k[i + 1].copy_(staging[i, ..., :K_DIM])
                        host_rope[i + 1].copy_(staging[i, ..., K_DIM:])
                    host_indices = torch.arange(
                        PAGE_SIZE, (n + 1) * PAGE_SIZE, dtype=torch.int64
                    )
                    device_indices = torch.tensor(
                        [
                            slot * PAGE_SIZE + token
                            for slot in slots
                            for token in range(PAGE_SIZE)
                        ],
                        dtype=torch.int64,
                    )
                    transfer_kv_dim_exchange(
                        device_indices=device_indices,
                        host_indices=host_indices,
                        device_k=device_k,
                        host_k=host_k,
                        device_v=device_rope,
                        host_v=host_rope,
                        device_index_k=None,
                        host_index_k=None,
                        page_size=PAGE_SIZE,
                        direction=TransferDirection.H2D,
                    )
                else:
                    read_pages(
                        store,
                        keys[start : start + n],
                        [direct[i].data_ptr() for i in range(n)],
                    )
                    for i, slot in enumerate(slots):
                        device_k[:, slot].copy_(direct[i, ..., :K_DIM])
                        device_rope[:, slot].copy_(direct[i, ..., K_DIM:])
                torch.npu.synchronize()
                for page, slot in zip(pages, slots):
                    check_page(device_k, device_rope, slot, page, torch)
                if (start + n) % 128 == 0:
                    print(f"CHECK_PROGRESS path={path} verified={start + n}/{count}", flush=True)
            print(f"{path}_OK pages={count}", flush=True)
        print("FEASIBILITY_OK", flush=True)
        print("P0" if args.size == "small" else "P1", flush=True)
    except Exception as exc:
        print(f"FEASIBILITY_FAIL stage={stage} error={exc!r}", flush=True)
        traceback.print_exc()
        print(failure_code(stage), flush=True)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        print("F9", flush=True)
        raise SystemExit(1)
