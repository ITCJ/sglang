"""A3 single-layer index-K H2D baseline; run through run_transfer_bench.sh.

Like workspace/kv_path_bench/kv_transfer_bench.py, allocation/initialization
and correctness checking are outside synchronized, complete-transfer timing.
This intentionally measures contiguous pinned copy_, not Mooncake/ADXL or
SGLang's paged transfer kernel. Every rank copies a full index-K replica.
"""

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import time
from datetime import timedelta
from importlib import metadata
from pathlib import Path


def summarize_ms(values):
    return {
        "p50_ms": statistics.median(values),
        "p95_ms": sorted(values)[math.ceil(0.95 * len(values)) - 1],
        "min_ms": min(values),
        "max_ms": max(values),
    }


def run(args):
    import torch
    import torch.distributed as dist
    import torch_npu  # noqa: F401

    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    nbytes = args.batch_size * args.context_len * args.index_head_dim * args.element_bytes
    chunk_bytes = args.chunk_bytes or nbytes
    result = {
        "status": "running", "rank": rank, "local_rank": local_rank,
        "world_size": world_size, "config": vars(args),
        "bytes_per_layer_per_rank": nbytes,
        "logical_bytes_all_layers_per_rank": nbytes * args.layers,
        "aggregate_copy_bytes_per_iteration": nbytes * world_size,
        "allocated_pinned_bytes_per_rank": nbytes * args.host_buffers,
        "allocated_device_bytes_per_rank": nbytes,
        "copies_per_iteration": math.ceil(nbytes / chunk_bytes),
        "protocol": "contiguous_pinned_H2D_full_replica_per_rank_no_inference",
        "timing": "rank barrier outside timing; event stream span and synchronized host wall time",
        "numa_nodes_requested": os.environ.get("NUMA_NODES", ""),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "hostname": platform.node(),
        "ascend_rt_visible_devices": os.environ.get("ASCEND_RT_VISIBLE_DEVICES"),
        "samples": [],
    }
    initialized = False
    try:
        visible_devices = torch.npu.device_count()
        result["visible_devices"] = visible_devices
        if visible_devices < world_size:
            raise RuntimeError(f"Need {world_size} visible logical NPUs, found {visible_devices}")
        if world_size > 1 and not dist.is_gloo_available():
            raise RuntimeError("16-rank copy benchmark requires the Gloo CPU backend")
        torch.npu.set_device(local_rank)
        if world_size > 1:
            # CPU-only coordination: no HCCL traffic in the copy timing window.
            dist.init_process_group("gloo", timeout=timedelta(seconds=300))
            initialized = True

        def barrier():
            if initialized:
                dist.barrier()

        result["device_name"] = torch.npu.get_device_name(local_rank)
        result["versions"] = {}
        for package in ("torch", "torch-npu", "sglang"):
            try:
                result["versions"][package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                result["versions"][package] = "unknown"
        result["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        host_buffers = [
            torch.empty(nbytes, dtype=torch.uint8, device="cpu", pin_memory=True)
            for _ in range(args.host_buffers)
        ]
        if not all(buf.is_pinned() for buf in host_buffers):
            raise RuntimeError("Host buffers must actually be pinned")
        for i, buf in enumerate(host_buffers):
            buf.fill_((rank + i) % 250 + 1)
        device_buffer = torch.empty(nbytes, dtype=torch.uint8, device=f"npu:{local_rank}")
        stream = torch.npu.Stream()
        start_event = torch.npu.Event(enable_timing=True)
        end_event = torch.npu.Event(enable_timing=True)
        # Precompute tensor views outside timing; optional chunks add copy
        # submission overhead, but do NOT simulate scattered-page gathering.
        copy_pairs = [
            [(device_buffer[offset:offset + chunk_bytes], host[offset:offset + chunk_bytes])
             for offset in range(0, nbytes, chunk_bytes)]
            for host in host_buffers
        ]
        torch.npu.synchronize()

        def submit(slot):
            for dst, src in copy_pairs[slot]:
                dst.copy_(src, non_blocking=True)

        # Verify each source buffer once before timing, bounded CPU scratch.
        for slot, host in enumerate(host_buffers):
            with torch.npu.stream(stream):
                device_buffer.zero_()
                submit(slot)
            stream.synchronize()
            for offset in range(0, nbytes, 16 * 1024 * 1024):
                end = min(nbytes, offset + 16 * 1024 * 1024)
                if not torch.equal(device_buffer[offset:end].cpu(), host[offset:end]):
                    raise RuntimeError(f"Copy validation failed at slot={slot}, offset={offset}")
        result["validation"] = "all_source_buffers_checked_before_timing"

        print(f"rank={rank} device={local_rank} bytes/layer={nbytes} "
              f"MiB/layer={nbytes / 2**20:.2f} full_replica=True", flush=True)
        for iteration in range(args.warmup + args.repeats):
            slot = iteration % args.host_buffers
            torch.npu.synchronize()
            barrier()
            # Wall time includes event submission, copy launch and completion
            # wait. Event span can include gaps between chunk submissions.
            with torch.npu.stream(stream):
                begin = time.perf_counter()
                start_event.record()
                submit(slot)
                end_event.record()
            end_event.synchronize()
            wall_ms = (time.perf_counter() - begin) * 1000
            event_ms = start_event.elapsed_time(end_event)
            if iteration >= args.warmup:
                result["samples"].append({
                    "iteration": iteration - args.warmup,
                    "wall_ms": wall_ms, "event_ms": event_ms,
                })

        result["wall"] = summarize_ms([s["wall_ms"] for s in result["samples"]])
        result["event"] = summarize_ms([s["event_ms"] for s in result["samples"]])
        result["effective_GBps_wall_p50"] = nbytes / (result["wall"]["p50_ms"] * 1e6)
        result["status"] = "ok"
        (output / f"rank_{rank:02d}.json").write_text(json.dumps(result, indent=2) + "\n")
        gathered = [None] * world_size
        if initialized:
            dist.all_gather_object(gathered, result)
        else:
            gathered[0] = result
        if rank == 0:
            slowest_samples = [
                max(worker["samples"][i]["wall_ms"] for worker in gathered)
                for i in range(args.repeats)
            ]
            slowest = summarize_ms(slowest_samples)
            summary = {
                "status": "ok", "config": vars(args), "world_size": world_size,
                "bytes_per_layer_per_rank": nbytes,
                "slowest_rank_per_iteration_wall": slowest,
                "slowest_rank_per_iteration_wall_ms": slowest_samples,
                "per_rank": [{
                    "rank": worker["rank"], "wall": worker["wall"],
                    "event": worker["event"],
                    "effective_GBps_wall_p50": worker["effective_GBps_wall_p50"],
                } for worker in gathered],
                "aggregate_effective_GBps_estimate": nbytes * world_size / (slowest["p50_ms"] * 1e6),
                "aggregate_note": "sum of replica bytes / max per-rank wall duration; CPU barrier start skew is not measured",
                "limitations": [
                    "No inference, MoE, main-KV fetch or HCCL contention",
                    "Concurrent rank transfers still contend with one another",
                    "Contiguous pinned copy, no paged packing, ADXL or Mooncake",
                    "Preallocated rotating host buffers, not full 61-layer host working set",
                    "layers is byte accounting only; one layer is timed per iteration",
                ],
            }
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary, indent=2), flush=True)
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = repr(exc)
        raise
    finally:
        (output / f"rank_{rank:02d}.json").write_text(json.dumps(result, indent=2) + "\n")
        if initialized:
            dist.destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=11)
    parser.add_argument("--context-len", type=int, default=65536)
    parser.add_argument("--index-head-dim", type=int, default=128)
    parser.add_argument("--element-bytes", type=int, default=2)
    parser.add_argument("--layers", type=int, default=61)
    parser.add_argument("--host-buffers", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--chunk-bytes", type=int, default=0)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if min(args.batch_size, args.context_len, args.index_head_dim, args.element_bytes,
           args.layers, args.host_buffers, args.repeats) < 1:
        parser.error("sizes, buffers and repeats must be positive")
    if args.warmup < 0 or args.chunk_bytes < 0:
        parser.error("warmup and chunk-bytes must be nonnegative")
    run(args)
