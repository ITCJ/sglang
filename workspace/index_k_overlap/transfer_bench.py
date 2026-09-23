"""A3 single-layer index-K H2D baseline; run through run_transfer_bench.sh.

Allocation/initialization and optional correctness checking are outside
the timed enqueue-and-complete batch.
This intentionally measures contiguous pinned copy_, not Mooncake/ADXL or
SGLang's paged transfer kernel. Every rank copies a full index-K replica.
"""

import argparse
import json
import math
import os
import platform
import subprocess
import time
from datetime import timedelta
from importlib import metadata
from pathlib import Path


def run(args):
    import torch
    import torch.distributed as dist
    import torch_npu  # noqa: F401

    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    batch_sizes = args.batch_sizes or [args.batch_size]
    max_nbytes = max(batch_sizes) * args.context_len * args.index_head_dim * args.element_bytes
    base_result = {
        "status": "running", "rank": rank, "local_rank": local_rank,
        "world_size": world_size, "config": vars(args),
        "allocated_pinned_bytes_per_rank": max_nbytes * args.host_buffers,
        "allocated_device_bytes_per_rank": max_nbytes,
        "protocol": "contiguous_pinned_H2D_full_replica_per_rank_no_inference",
        "timing": "warmup sync and rank barrier outside timing; enqueue all repeats then synchronize once; total / repeats",
        "numa_binding": "none; system memory policy",
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "hostname": platform.node(),
        "ascend_rt_visible_devices": os.environ.get("ASCEND_RT_VISIBLE_DEVICES"),
        "validation_enabled": args.validate,
    }
    result = dict(base_result, correct=None)
    rank_path = output / f"rank_{rank:02d}.json"
    initialized = False
    try:
        visible_devices = torch.npu.device_count()
        base_result["visible_devices"] = visible_devices
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

        base_result["device_name"] = torch.npu.get_device_name(local_rank)
        base_result["versions"] = {}
        for package in ("torch", "torch-npu", "sglang"):
            try:
                base_result["versions"][package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                base_result["versions"][package] = "unknown"
        base_result["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent,
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        host_buffers = [
            torch.empty(max_nbytes, dtype=torch.uint8, device="cpu", pin_memory=True)
            for _ in range(args.host_buffers)
        ]
        if not all(buf.is_pinned() for buf in host_buffers):
            raise RuntimeError("Host buffers must actually be pinned")
        for i, buf in enumerate(host_buffers):
            buf.fill_((rank + i) % 250 + 1)
        device_buffer = torch.empty(max_nbytes, dtype=torch.uint8, device=f"npu:{local_rank}")
        stream = torch.npu.Stream()
        start_event = torch.npu.Event(enable_timing=True)
        end_event = torch.npu.Event(enable_timing=True)
        torch.npu.synchronize()

        for bs in batch_sizes:
            nbytes = bs * args.context_len * args.index_head_dim * args.element_bytes
            chunk_bytes = args.chunk_bytes or nbytes
            prefix = f"bs{bs}_" if args.batch_sizes else ""
            rank_path = output / f"{prefix}rank_{rank:02d}.json"
            summary_path = output / f"{prefix}summary.json"
            result = dict(
                base_result, status="running", batch_size=bs,
                bytes_per_layer_per_rank=nbytes,
                logical_bytes_all_layers_per_rank=nbytes * args.layers,
                aggregate_copy_bytes_per_iteration=nbytes * world_size,
                copies_per_iteration=math.ceil(nbytes / chunk_bytes),
                correct=None,
            )
            # Views are created outside timing; chunks still add submission cost.
            copy_pairs = [
                [(device_buffer[offset:offset + chunk_bytes], host[offset:offset + chunk_bytes])
                 for offset in range(0, nbytes, chunk_bytes)]
                for host in host_buffers
            ]

            def submit(slot):
                for dst, src in copy_pairs[slot]:
                    dst.copy_(src, non_blocking=True)

            if args.validate:
                # Check each source buffer once, outside timing.
                for slot, host in enumerate(host_buffers):
                    with torch.npu.stream(stream):
                        device_buffer[:nbytes].zero_()
                        submit(slot)
                    stream.synchronize()
                    for offset in range(0, nbytes, 16 * 1024 * 1024):
                        end = min(nbytes, offset + 16 * 1024 * 1024)
                        if not torch.equal(device_buffer[offset:end].cpu(), host[offset:end]):
                            raise RuntimeError(f"Copy validation failed at bs={bs}, slot={slot}, offset={offset}")
                result["correct"] = True

            print(f"rank={rank} device={local_rank} bs={bs} bytes/layer={nbytes} "
                  f"MiB/layer={nbytes / 2**20:.2f} full_replica=True", flush=True)
            for iteration in range(args.warmup):
                slot = iteration % args.host_buffers
                with torch.npu.stream(stream):
                    submit(slot)
            stream.synchronize()
            barrier()
            with torch.npu.stream(stream):
                begin = time.perf_counter()
                start_event.record()
                for iteration in range(args.repeats):
                    submit(iteration % args.host_buffers)
                end_event.record()
            end_event.synchronize()
            wall_total_ms = (time.perf_counter() - begin) * 1000
            event_total_ms = start_event.elapsed_time(end_event)
            result["wall_total_ms"] = wall_total_ms
            result["wall_mean_ms"] = wall_total_ms / args.repeats
            result["event_total_ms"] = event_total_ms
            result["event_mean_ms"] = event_total_ms / args.repeats
            result["effective_GBps_wall_mean"] = nbytes / (result["wall_mean_ms"] * 1e6)
            result["status"] = "ok"
            rank_path.write_text(json.dumps(result, indent=2) + "\n")
            gathered = [None] * world_size
            if initialized:
                dist.all_gather_object(gathered, result)
            else:
                gathered[0] = result
            if rank == 0:
                slowest_wall_total_ms = max(worker["wall_total_ms"] for worker in gathered)
                slowest_wall_mean_ms = slowest_wall_total_ms / args.repeats
                summary = {
                    "status": "ok", "config": vars(args), "batch_size": bs,
                    "world_size": world_size,
                    "measurement_protocol": "batched_async_sync_once_v1",
                    "validation_enabled": args.validate,
                    "correct": True if args.validate else None,
                    "bytes_per_layer_per_rank": nbytes,
                    "allocated_pinned_bytes_per_rank": max_nbytes * args.host_buffers,
                    "allocated_device_bytes_per_rank": max_nbytes,
                    "slowest_rank_wall_total_ms": slowest_wall_total_ms,
                    "slowest_rank_wall_mean_ms": slowest_wall_mean_ms,
                    "per_rank": [{
                        "rank": worker["rank"],
                        "wall_total_ms": worker["wall_total_ms"],
                        "wall_mean_ms": worker["wall_mean_ms"],
                        "event_total_ms": worker["event_total_ms"],
                        "event_mean_ms": worker["event_mean_ms"],
                        "effective_GBps_wall_mean": worker["effective_GBps_wall_mean"],
                    } for worker in gathered],
                    "aggregate_effective_GBps_estimate": nbytes * world_size / (slowest_wall_mean_ms * 1e6),
                    "aggregate_note": "sum of replica bytes / slowest rank mean wall time; CPU barrier start skew is not measured",
                    "limitations": [
                        "No inference, MoE, main-KV fetch or HCCL contention",
                        "Concurrent rank transfers still contend with one another",
                        "Contiguous pinned copy, no paged packing, ADXL or Mooncake",
                        "Full-size buffers reused for all batch sizes, not a 61-layer host working set",
                        "layers is byte accounting only; one layer is timed per iteration",
                    ],
                }
                summary_path.write_text(json.dumps(summary, indent=2) + "\n")
                print(f"Completed bs={bs} ranks={world_size}: slowest mean={slowest_wall_mean_ms:.3f} ms", flush=True)
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = repr(exc)
        raise
    finally:
        rank_path.write_text(json.dumps(result, indent=2) + "\n")
        if initialized:
            dist.destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=11)
    parser.add_argument("--batch-sizes", type=int, nargs="+",
                        help="run all listed batch sizes in one worker group")
    parser.add_argument("--context-len", type=int, default=65536)
    parser.add_argument("--index-head-dim", type=int, default=128)
    parser.add_argument("--element-bytes", type=int, default=2)
    parser.add_argument("--layers", type=int, default=61)
    parser.add_argument("--host-buffers", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--chunk-bytes", type=int, default=0)
    parser.add_argument("--validate", action="store_true",
                        help="check every host buffer before timing (default: off)")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if min(args.batch_size, args.context_len, args.index_head_dim, args.element_bytes,
           args.layers, args.host_buffers, args.repeats) < 1:
        parser.error("sizes, buffers and repeats must be positive")
    if args.warmup < 0 or args.chunk_bytes < 0:
        parser.error("warmup and chunk-bytes must be nonnegative")
    if args.batch_sizes and (min(args.batch_sizes) < 1 or len(set(args.batch_sizes)) != len(args.batch_sizes)):
        parser.error("batch-sizes must be positive and unique")
    run(args)
