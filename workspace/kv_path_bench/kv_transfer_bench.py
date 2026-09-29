#!/usr/bin/env python3
"""Measure L2->L1 and L3->L2->L1 for whole requests without extra staging."""

import argparse
import ctypes
from importlib import metadata
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

from bench_ports import MASTER_PORT
from feasibility_check import check_page
from feasibility_log import enable_log, print_result
from kv_layout import K_DIM, LAYERS, PAGE_BYTES, PAGE_SIZE, ROPE_DIM, page_keys, split_page_payload


GIB = 1 << 30
WARMUP = 2
REPEATS = 10
from path_names import PATH_NAMES

PATHS = {code: PATH_NAMES[code] for code in "ACM"}


def physical_page_slots(count: int, layout: str) -> int:
    """Number of addressable slots, including reserved slot zero."""
    if count < 1:
        raise ValueError("page counts must be positive")
    if layout == "contiguous":
        return count + 1
    if layout == "scattered":
        return count * 2
    raise ValueError(f"unknown layout: {layout}")


def make_batches(count: int, batch_pages: int | None = None, layout: str = "scattered") -> list[dict]:
    """Build logical pages and physical slots; scattered slots have page-sized gaps."""
    batch_pages = count if batch_pages is None else batch_pages
    if count < 1 or batch_pages < 1:
        raise ValueError("page counts must be positive")
    if layout not in ("contiguous", "scattered"):
        raise ValueError(f"unknown layout: {layout}")
    slots = [1 + (2 * page if layout == "scattered" else page)
             for page in range(count)]
    return [
        {
            "pages": list(range(start, min(start + batch_pages, count))),
            "slots": slots[start:min(start + batch_pages, count)],
            "layout": layout,
        }
        for start in range(0, count, batch_pages)
    ]


def measure_batch(action, prepare, synchronize, warmup, repeats, clock=time.perf_counter, validate=None):
    """Exclude input/reset preparation; include completion of each complete action."""
    samples = []
    for iteration in range(warmup + repeats):
        prepare()
        synchronize()
        start = clock()
        action()
        synchronize()
        elapsed = clock() - start
        if validate is not None:
            validate()
        if iteration >= warmup:
            samples.append(elapsed)
    return samples


def summarize(code: str, samples: list[float], nbytes: int, validate: bool = False,
              path_name: str | None = None) -> dict:
    median = statistics.median(samples)
    return {
        "path": path_name or PATHS[code], "median_s": median,
        "p95_s": sorted(samples)[math.ceil(0.95 * len(samples)) - 1],
        "effective_gbps": nbytes / median / 1e9,
        "samples_s": samples, "validation_enabled": validate, "correct": True if validate else None,
        "measurement_protocol": "whole_request_v2",
    }


def run_unidex(args, result, batches, physical_slots):
    from unidex_engine import HOST_MEMORY, PATH_NAME, UnidexEngine, configure_soc

    engine = None
    stage = "A3 SOC configuration"
    failure = "F9"
    result.update(engine="unidex", host_memory=HOST_MEMORY, l2_only=True,
                  path_order="A only", store_local_buffer_bytes=0,
                  object_layout="local logical pages; no Store objects accessed",
                  l2_layout="page,layer,token,1,dim; separate KV/RoPE in SysV registered Host",
                  disabled_paths=["all L3 paths"], block_dim=args.block_dim)
    try:
        configure_soc()
        stage = "target imports"
        import torch
        import torch_npu  # noqa: F401

        result["versions"] = {}
        for package in ("torch", "torch-npu", "sgl-kernel-npu"):
            try:
                result["versions"][package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                result["versions"][package] = "unknown"
        failure, stage = "F2", "single-device NPU L1 allocation"
        torch.npu.set_device(args.device)
        device_k = torch.empty((LAYERS, physical_slots, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        device_rope = torch.empty((LAYERS, physical_slots, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        failure, stage = "F3", "SysV registered Host allocation"
        engine = UnidexEngine(torch, args.device, args.block_dim)
        engine.allocate(physical_slots)
        item = batches[0]
        pages, slots = item["pages"], item["slots"]
        host_k, host_rope = engine.host_k, engine.host_rope
        stage = "fixed Host source preparation"
        start = time.perf_counter()
        k_page_elements = LAYERS * PAGE_SIZE * K_DIM
        for page, slot in zip(pages, slots):
            packed = torch.frombuffer(bytearray(split_page_payload(page)), dtype=torch.bfloat16)
            host_k[slot].copy_(packed[:k_page_elements].view_as(host_k[slot]))
            host_rope[slot].copy_(packed[k_page_elements:].view_as(host_rope[slot]))
        result["host_source_prepare_s"] = time.perf_counter() - start
        stage = "unidex index preparation"
        engine.prepare_indices(pages, slots, device_k, device_rope)
        result.update(engine.metadata)

        def prepare():
            device_k.zero_()
            device_rope.zero_()

        def validate():
            for page, slot in zip(pages, slots):
                check_page(device_k, device_rope, slot, page, torch)
            used = set(slots)
            for slot in range(physical_slots):
                if slot in used:
                    continue
                for component in (device_k[:, slot], device_rope[:, slot], host_k[slot], host_rope[slot]):
                    if torch.count_nonzero(component.contiguous().view(torch.uint8)).item():
                        raise RuntimeError(f"unused/reserved Host or NPU page {slot} overwritten")

        failure, stage = "F5", "unidex whole-request samples and completion"
        samples = measure_batch(engine.submit, prepare, torch.npu.synchronize,
                                args.warmup, args.repeats, validate=validate if args.validate else None)
        path = summarize("A", samples, result["bytes"], validate=args.validate, path_name=PATH_NAME)
        path.update(engine="unidex", host_memory=HOST_MEMORY, block_dim=args.block_dim)
        result["paths"].append(path)
        result["status"] = "ok"
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="failed", stage=stage, error=repr(exc), failure_code=failure)
        if engine is not None:
            result["engine_stage"] = engine.stage
        print(f"PERFORMANCE_FAIL stage={stage} error={exc!r}", flush=True)
        traceback.print_exc()
    finally:
        if engine is not None:
            result.update(engine.metadata)
            try:
                engine.close()
                result.update(engine.metadata)
            except (Exception, KeyboardInterrupt) as exc:
                result.update(status="failed", cleanup_error=repr(exc), failure_code="F9")
                result.setdefault("stage", "unidex completion/cleanup")
                result.setdefault("error", repr(exc))
                result.update(engine.metadata)
                print(f"CLEANUP_FAIL {exc!r}; owners retained until process exit", flush=True)
                traceback.print_exc()
    return result.get("failure_code", failure)


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
    parser.add_argument("--validate", action="store_true", help="validate all KV bytes after every iteration (default: off)")
    parser.add_argument("--warmup", type=int, default=WARMUP)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--copy-engine", choices=("sglkernel", "unidex"), default="sglkernel")
    parser.add_argument("--l2-only", action="store_true", help="single-device unidex path; no Store or IPs")
    parser.add_argument("--block-dim", type=int, default=24, help="unidex AI Core block_dim (default: 24)")
    parser.add_argument("--skip-direct", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    local_ip, master_ip = args.local_ip or args.client_ip, args.master_ip or args.store_ip
    if args.copy_engine == "unidex" and not args.l2_only:
        parser.error("unidex requires --l2-only; L3 modes are not implemented")
    if args.l2_only and args.copy_engine != "unidex":
        parser.error("--l2-only currently requires --copy-engine unidex")
    if not args.l2_only and (not local_ip or not master_ip):
        parser.error("provide client and Store IPs")
    tokens = args.tokens if args.tokens is not None else {"small": 128, "max": 131072, None: 1024}[args.size]
    if args.size and args.tokens is not None and tokens != {"small": 128, "max": 131072}[args.size]:
        parser.error("size and --tokens disagree")
    if not PAGE_SIZE <= tokens <= 131072 or tokens % PAGE_SIZE:
        parser.error("--tokens must be a multiple of 128 between 128 and 131072")
    if args.device < 0 or args.warmup < 0 or args.repeats < 1 or not 1 <= args.block_dim <= (1 << 32) - 1:
        parser.error("device/warmup must be nonnegative; repeats must be positive")

    name = f"a3-kv-perf-{'unidex-sysv_registered-' if args.copy_engine == 'unidex' else ''}{tokens}"
    output = args.output or Path(f"/tmp/{name}.json")
    enable_log("perf", str(tokens), path=args.log or Path(f"/tmp/{name}.log"))
    if not args.l2_only:
        os.environ.setdefault("ASCEND_ENABLE_USE_FABRIC_MEM", "1")
        os.environ.setdefault("HCCL_INTRA_ROCE_ENABLE", "0")


    count = tokens // PAGE_SIZE
    physical_slots = physical_page_slots(count, args.layout)
    k_page_elements = LAYERS * PAGE_SIZE * K_DIM
    k_page_bytes = k_page_elements * 2
    rope_page_bytes = PAGE_BYTES - k_page_bytes
    host_shape = (physical_slots, LAYERS, PAGE_SIZE, 1)
    l2_bytes = physical_slots * PAGE_BYTES
    local_buffer_bytes = max(GIB, math.ceil((l2_bytes + PAGE_BYTES) / GIB) * GIB)
    capacity = max(4, math.ceil(local_buffer_bytes / GIB) + 2)
    if not args.l2_only:
        os.environ.setdefault("ASCEND_GLOBAL_RESOURCE_CONFIG", json.dumps({"fabric_memory.max_capacity": capacity}))
    if not args.l2_only and int(json.loads(os.environ["ASCEND_GLOBAL_RESOURCE_CONFIG"]).get("fabric_memory.max_capacity", 0)) < capacity:
        raise RuntimeError(f"whole-request Host pool needs fabric_memory.max_capacity >= {capacity}")
    keys = page_keys(args.prefix + "-split", count)
    batches = make_batches(count, layout=args.layout)
    result = {
        "status": "running", "tokens": tokens, "pages": count, "page_bytes": PAGE_BYTES,
        "engine": "sglkernel", "host_memory": "adxl", "l2_only": False,
        "layout": args.layout,
        "bytes": count * PAGE_BYTES, "request_pages": count,
        "measurement_protocol": "whole_request_v2",
        "warmup": args.warmup, "repeats": args.repeats, "validation_enabled": args.validate,
        "object_layout": "one key per page: all compressed KV, then all RoPE",
        "l2_layout": "page,layer,token,1,dim; separate KV/RoPE in ADXL Host buffer",
        "l1_layout": "layer,page,token,1,dim; separate KV/RoPE",
        "l1_slots": [slot for item in batches for slot in item["slots"]],
        "l2_slots": [slot for item in batches for slot in item["slots"]],
        "mapping_version": "page_gap_v2",
        "timing": "one synchronized whole-request wall time per sample",
        "path_order": "A/C/M; each path warmed as a complete request",
        "l2_bytes": l2_bytes, "allocated_host_staging_bytes": 0,
        "store_local_buffer_bytes": local_buffer_bytes,
        "physical_page_slots": physical_slots,
        "l1_bytes": physical_slots * PAGE_BYTES,
        "disabled_paths": ["L3->NPU staging->L1"], "paths": [],
        "commit": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
            capture_output=True, text=True,
        ).stdout.strip(),
    }
    if args.copy_engine == "unidex":
        result.update(engine="unidex", host_memory="sysv_registered", l2_only=True,
                      actual_command=[sys.executable, *sys.argv], process_pid=os.getpid(),
                      process_started_wall_s=time.time(), working_directory=os.getcwd())
        output.write_text(json.dumps(result, indent=2) + "\n")

        def terminate(signum, frame):
            raise RuntimeError(f"received signal {signum}; stop this request")

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            failure = run_unidex(args, result, batches, physical_slots)
        finally:
            signal.signal(signal.SIGTERM, previous)
        output.write_text(json.dumps(result, indent=2) + "\n")
        if result["status"] != "ok":
            print(failure, flush=True)
            return 1
        item = result["paths"][0]
        print_result(f"tokens={tokens} layout={args.layout} {item['path']}={item['median_s'] * 1000:.3f} ms "
                     f"validation={args.validate} launches={result['launch_count']}")
        return 0
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
        device_k = torch.empty((LAYERS, physical_slots, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        device_rope = torch.empty((LAYERS, physical_slots, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        store = MooncakeDistributedStore()
        rc = store.setup(local_ip, "P2PHANDSHAKE", 0, local_buffer_bytes, "ascend", "", f"{master_ip}:{args.port}")
        if rc != 0:
            raise RuntimeError(f"Store setup returned {rc}")

        failure, stage = "F3", "whole-request ADXL L2 allocation"
        pool = BufferPool(store, block_on_exhaustion=False)

        def host_tensor(nbytes):
            lease = pool.acquire(nbytes)
            leases.append(lease)
            backing = (ctypes.c_byte * nbytes).from_address(int(lease.ptr))
            return torch.frombuffer(backing, dtype=torch.bfloat16)

        l2 = host_tensor(l2_bytes)
        k_elements = physical_slots * k_page_elements
        host_k = l2[:k_elements].view(*host_shape, K_DIM)
        host_rope = l2[k_elements:].view(*host_shape, ROPE_DIM)
        item = batches[0]
        pages, slots = item["pages"], item["slots"]
        host_indices = torch.tensor(
            [slot * PAGE_SIZE + token for slot in slots for token in range(PAGE_SIZE)],
            dtype=torch.int64)
        device_indices = torch.tensor(
            [slot * PAGE_SIZE + token for slot in slots for token in range(PAGE_SIZE)],
            dtype=torch.int64)
        target_ptrs = [[host_k[slot].data_ptr(), host_rope[slot].data_ptr()] for slot in slots]
        target_sizes = [[k_page_bytes, rope_page_bytes] for _ in pages]

        def expected(page):
            return torch.frombuffer(bytearray(split_page_payload(page)), dtype=torch.bfloat16)

        def run_path(code):
            if code in ("C", "M"):
                rc = list(store.batch_get_into_multi_buffers(keys, target_ptrs, target_sizes))
                if rc != [PAGE_BYTES] * count:
                    raise RuntimeError(f"whole-request L2 read failed: {rc}")
            if code != "M":
                transfer_kv_dim_exchange(
                    device_indices=device_indices, host_indices=host_indices,
                    device_k=device_k, host_k=host_k, device_v=device_rope, host_v=host_rope,
                    device_index_k=None, host_index_k=None, page_size=PAGE_SIZE,
                    direction=TransferDirection.H2D)

        for code in PATHS:
            failure, stage = "F5", f"whole-request performance {PATHS[code]}"
            if code == "A":
                for page in pages:
                    packed = expected(page)
                    slot = slots[page]
                    host_k[slot].copy_(packed[:k_page_elements].view_as(host_k[slot]))
                    host_rope[slot].copy_(packed[k_page_elements:].view_as(host_rope[slot]))

            def prepare():
                device_k.zero_()
                device_rope.zero_()
                if code != "A":
                    host_k.zero_()
                    host_rope.zero_()

            def validate():
                if code == "M":
                    for page, slot in zip(pages, slots):
                        packed = expected(page)
                        for actual, reference in (
                            (host_k[slot].reshape(-1), packed[:k_page_elements]),
                            (host_rope[slot].reshape(-1), packed[k_page_elements:])):
                            if not torch.equal(actual.view(torch.uint8), reference.view(torch.uint8)):
                                raise RuntimeError("L2 content mismatch")
                    guards = [slot for slot in range(physical_slots) if slot not in set(slots)]
                    if (torch.count_nonzero(host_k[guards]).item()
                            or torch.count_nonzero(host_rope[guards]).item()):
                        raise RuntimeError("unused L2 guard page overwritten")
                else:
                    for page, slot in zip(pages, slots):
                        check_page(device_k, device_rope, slot, page, torch)
                    if torch.count_nonzero(device_k[:, 0]).item() or torch.count_nonzero(device_rope[:, 0]).item():
                        raise RuntimeError("reserved L1 page overwritten")
                    guards = [slot for slot in range(physical_slots) if slot not in set(slots)]
                    if (torch.count_nonzero(device_k[:, guards]).item()
                            or torch.count_nonzero(device_rope[:, guards]).item()):
                        raise RuntimeError("unused L1 guard page overwritten")

            samples = measure_batch(lambda: run_path(code), prepare, torch.npu.synchronize,
                                    args.warmup, args.repeats, validate=validate if args.validate else None)
            result["paths"].append(summarize(code, samples, count * PAGE_BYTES, validate=args.validate))
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
    for item in result["paths"]:
        print_result(f"tokens={tokens} layout={args.layout} {item['path']}={item['median_s'] * 1000:.3f} ms")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        print("F9", flush=True)
        raise SystemExit(1)
