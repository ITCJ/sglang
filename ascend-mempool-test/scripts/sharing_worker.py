"""One NPU worker for run_numa_sharing.py; never start manually."""

from __future__ import annotations

import argparse
import datetime
import faulthandler
import importlib
import json
import math
import os
import platform
import signal
import sys
import time
import traceback
from functools import partial
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verify_graph import load_runtime, stage_local_kv

from ascend_mempool.sharing import SharingPlan, port_plan
from ascend_mempool.sharing_cases import Case, cases
from ascend_mempool.sharing_control import Cohort
from ascend_mempool.sharing_copy import CopyWorkload
from ascend_mempool.sharing_pipeline import Pipeline
from ascend_mempool.sharing_pool import SharingPool, check_numa_placement, cpu_list


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def checkpoint(output: Path, label: str) -> None:
    """Keep the last host boundary and dump stacks after a minute without progress."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save(output / "progress.json", dict(phase=label, timestamp=now))
    print(f"[DEBUG-numa-sharing] {now} {label}", flush=True)
    faulthandler.dump_traceback_later(60, repeat=True)


def check_work(
    work: CopyWorkload,
    pipeline: Pipeline,
    cohort: Cohort,
    torch: Any,
    invoke: Any,
    label: str,
    variant: int,
    *,
    empty: bool = False,
    progress: Callable[[str], None] = lambda label: None,
) -> None:
    progress(label + "/load")
    work.load(variant, empty=empty)
    pipeline.update()
    progress(label + "/metadata-sync")
    torch.npu.synchronize()
    progress(label + "/reset-ready")
    cohort.exchange(label + "/reset-ready")
    progress(label + "/reset-write")
    if work.has_write:
        work.reset_write_region(torch)
    progress(label + "/reset-done")
    cohort.exchange(label + "/reset-done")
    progress(label + "/launch")
    with torch.npu.stream(pipeline.main):
        pipeline.main.wait_stream(torch.npu.default_stream())
        pipeline.run(invoke)
    progress(label + "/device-sync")
    torch.npu.synchronize()
    progress(label + "/verify-selected")
    pipeline.verify()
    progress(label + "/copies-drained")
    cohort.exchange(label + "/copies-drained")
    progress(label + "/verify-writes")
    work.verify_writes(torch)
    progress(label + "/verified")
    cohort.exchange(label + "/verified")


def benchmark(
    pool: SharingPool, cohort: Cohort, config: dict[str, Any], torch: Any, output: Path
) -> list[dict[str, Any]]:
    results = []
    diagnostic = config.get("diagnostic", False)
    progress: Callable[[str], None] = (
        partial(checkpoint, output) if diagnostic else lambda label: None
    )
    engines = ["eager"] if config["smoke"] else ["eager", "graph"]
    for blocks in config["block_dims"]:
        for case, consumer_side in product(
            cases(config["smoke"], config.get("diagnose_write_read", False)), (0, 1)
        ):
            for engine in engines:
                label = f"{blocks}/{case.name}/consumer-{consumer_side}/{engine}"
                print(f"BEGIN {label}", flush=True)
                progress(label + "/create-workload")
                active_side = pool.rank == consumer_side
                work = CopyWorkload(
                    pool,
                    case,
                    topk=config["topk"],
                    block_dim=blocks,
                    side_active=active_side,
                )
                progress(label + "/create-pipeline")
                pipeline = Pipeline(work, torch, measure_selected=active_side)
                graph = None
                invoke = pipeline.launch
                if engine == "graph" and active_side:
                    progress(label + "/graph-warmup")
                    work.load(0, empty=True)
                    pipeline.update()
                    with torch.npu.stream(pipeline.main):
                        pipeline.main.wait_stream(torch.npu.default_stream())
                        for _ in range(config["warmup"]):
                            pipeline.run(pipeline.launch)
                    torch.npu.synchronize()
                    progress(label + "/graph-capture")
                    graph = torch.npu.NPUGraph()
                    with torch.npu.graph(
                        graph, stream=pipeline.main, auto_dispatch_capture=True
                    ):
                        pipeline.launch()
                    invoke = graph.replay
                for variant, empty in ((0, False), (1, True), (1, False), (2, False)):
                    check_work(
                        work,
                        pipeline,
                        cohort,
                        torch,
                        invoke,
                        f"{label}/correctness-{variant}-{empty}",
                        variant,
                        empty=empty,
                        progress=progress,
                    )
                progress(label + "/sample-warmup")
                with torch.npu.stream(pipeline.main):
                    for _ in range(config["warmup"]):
                        pipeline.run(invoke)
                torch.npu.synchronize()
                counts = work.byte_counts()
                for sample in range(config["samples"]):
                    progress(f"{label}/sample-{sample}")
                    repeats = config["repeats"]
                    event_pairs = [
                        (
                            torch.npu.Event(enable_timing=True),
                            torch.npu.Event(enable_timing=True),
                            torch.npu.Event(enable_timing=True),
                        )
                        for _ in range(repeats if active_side else 0)
                    ]
                    cohort.aligned_start(f"{label}/sample-{sample}")
                    if not active_side:
                        # Remain joined, but do not issue device work against
                        # either owner's banks during the other side's sample.
                        cohort.exchange(f"{label}/sample-{sample}/done")
                        continue
                    start_ns = time.monotonic_ns()
                    with torch.npu.stream(pipeline.main):
                        for timers in event_pairs:
                            pipeline.run(invoke, timers)
                    pipeline.main.synchronize()
                    done_ns = time.monotonic_ns()
                    durations = [
                        float(start.elapsed_time(end))
                        for start, selected, end in event_pairs
                    ]
                    selected_times = (
                        [
                            float(start.elapsed_time(selected))
                            for start, selected, end in event_pairs
                        ]
                        if pipeline.selected_signal is not None
                        else durations
                    )
                    if any(
                        not math.isfinite(value) or value <= 0
                        for value in durations + selected_times
                    ):
                        raise RuntimeError(
                            "device timer did not return a positive duration"
                        )
                    row = dict(
                        case=case.signature(),
                        engine=engine,
                        sample=sample,
                        member=pool.member,
                        side=pool.rank,
                        consumer_side=consumer_side,
                        opposite_side_idle=True,
                        mode=pool.mode,
                        round=config["round"],
                        repeats=repeats,
                        start_ns=start_ns,
                        done_ns=done_ns,
                        device_ms=sum(durations),
                        device_calls_ms=durations,
                        selected_calls_ms=selected_times,
                        selected_timing="captured external wait submitted after producer; fresh observer timestamp includes host submission and observer delay"
                        if pipeline.selected_signal is not None
                        else "same as complete copy",
                        fixture_cache_reset_in_host_envelope=case.operation
                        == "materialize",
                        publication_collective_in_device_time=case.operation
                        == "write_then_read",
                        **counts,
                    )
                    results.append(row)
                    # A barrier after every sample also bounds scratch lifetimes.
                    cohort.exchange(f"{label}/sample-{sample}/done")
                progress(label + "/post-check")
                pipeline.verify()
                work.verify_writes(torch)
                cohort.exchange(label + "/post-check")
                if work.has_write:
                    work.reset_write_region(torch)
                torch.npu.synchronize()
                cohort.exchange(label + "/restore")
                save(output / "samples.json", results)
                print(f"PASS {label}", flush=True)
                progress(label + "/release")
                del graph, invoke, pipeline, work
                torch.npu.empty_cache()
    return results


def delayed_publication(
    pool: SharingPool, cohort: Cohort, config: dict[str, Any], torch: Any, output: Path
) -> list[dict[str, Any]]:
    """Read current locally; after local writer->HCCL, read the shared full rows."""
    results = []
    for engine in ("eager", "graph"):
        case = Case("delayed-writer-publication", "current_parallel", active=2)
        work = CopyWorkload(
            pool, case, topk=config["topk"], block_dim=config["block_dims"][0]
        )
        pipeline = Pipeline(
            work, torch, delay_matmuls=config["delay_matmuls"], publication=True
        )
        pipeline.update()
        graph = None
        invoke = pipeline.launch
        if engine == "graph":
            with torch.npu.stream(pipeline.main):
                pipeline.main.wait_stream(torch.npu.default_stream())
                pipeline.prepare()
                pipeline.launch()
            torch.npu.synchronize()
            graph = torch.npu.NPUGraph()
            with torch.npu.graph(
                graph, stream=pipeline.main, auto_dispatch_capture=True
            ):
                pipeline.launch()
            invoke = graph.replay
        for variant in (0, 1):
            work.load(variant)
            pipeline.update()
            torch.npu.synchronize()
            cohort.exchange(f"delay/{engine}/{variant}/ready")
            work.reset_write_region(torch)
            cohort.exchange(f"delay/{engine}/{variant}/reset")
            with torch.npu.stream(pipeline.main):
                pipeline.main.wait_stream(torch.npu.default_stream())
                pipeline.prepare()
                invoke()
            early = None
            if engine == "eager" and pool.member == 1:
                pipeline.selected_ready.synchronize()
                early = not pipeline.writer_done.query()
                if not early:
                    raise AssertionError(
                        "delay fixture did not expose selected-ready before writer completion; increase --delay-matmuls"
                    )
            torch.npu.synchronize()
            pipeline.verify()
            results.append(
                dict(
                    engine=engine,
                    variant=variant,
                    selected_before_writer_done=early,
                    publication_readback_passed=True,
                    graph_early_order_evidence="separate trace"
                    if engine == "graph"
                    else "device event query",
                )
            )
            cohort.exchange(f"delay/{engine}/{variant}/done")
        # Profiling is a separate run and is NEVER merged into benchmark samples.
        torch_npu = importlib.import_module("torch_npu")
        with torch_npu.profiler.profile(
            activities=[
                torch_npu.profiler.ProfilerActivity.CPU,
                torch_npu.profiler.ProfilerActivity.NPU,
            ],
            record_shapes=True,
        ) as profiler:
            with torch.npu.stream(pipeline.main):
                pipeline.prepare()
                invoke()
            torch.npu.synchronize()
        profiler.export_chrome_trace(str(output / f"delayed-{engine}-trace.json"))
        pipeline.verify()
        cohort.exchange(f"delay/{engine}/profile-done")
        work.reset_write_region(torch)
        torch.npu.synchronize()
        cohort.exchange(f"delay/{engine}/restore")
        del graph, invoke, pipeline, work
    return results


def run(config: dict[str, Any], output: Path) -> int:
    report: dict[str, Any] = dict(status="running", config=config)
    cohort = None
    pool = None
    torch: Any = None
    mf: Any = None
    mf_ready = bm_ready = distributed_ready = False
    success = False
    try:
        affinity = cpu_list(
            Path(
                f"/sys/devices/system/node/node{config['numa_node']}/cpulist"
            ).read_text()
        )
        affinity &= os.sched_getaffinity(0)
        if not affinity:
            raise RuntimeError(
                "container CPU set has no CPUs on the requested NUMA node"
            )
        os.sched_setaffinity(0, affinity)
        runtime_args = SimpleNamespace(device_id=config["device_id"])
        torch, mf, environment = load_runtime(runtime_args)
        report["environment"] = dict(
            environment, kernel=platform.release(), cpu_affinity=sorted(affinity)
        )
        for path in (
            "/usr/local/Ascend/driver/version.info",
            "/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
        ):
            file = Path(path)
            report["environment"][path] = (
                file.read_text() if file.is_file() else "unavailable"
            )
        rank = config["side"] * 2 + config["member"]
        ports = port_plan(
            config["control_port"], config["mode"], config["side"], config["member"]
        )
        report["ports"] = ports
        cohort = Cohort(
            config["prefill_host"],
            ports["coordinator"],
            rank,
            config["identity"],
            config["timeout"],
        )
        versions = {
            name: environment[name]
            for name in ("memfabric", "torch", "torch_npu", "sgl_kernel_npu")
        }
        peers = cohort.exchange("environment", versions=versions)
        if any(peer["versions"] != versions for peer in peers):
            raise RuntimeError("worker SDK versions differ")
        mf.set_log_level(1)
        SharingPool.check(mf.initialize(), "initialize MF")
        mf_ready = True
        bm = mf.bm
        bm_config = bm.BmConfig()
        bm_config.auto_ranking = False
        bm_config.rank_id = rank if config["mode"] == "shared" else config["side"]
        bm_config.start_store = bm_config.rank_id == 0
        bm_config.init_timeout = bm_config.create_timeout = (
            bm_config.operation_timeout
        ) = int(config["timeout"])
        local_host = (
            config["prefill_host"] if config["side"] == 0 else config["decode_host"]
        )
        bm_config.set_nic(f"tcp://{local_host}:{ports['nic_base']}")
        store_port = ports["store"]
        if bm_config.rank_id != 0:
            SharingPool._wait_for_store(
                config["prefill_host"], store_port, config["timeout"]
            )
        SharingPool.check(
            bm.initialize(
                f"tcp://{config['prefill_host']}:{store_port}",
                4 if config["mode"] == "shared" else 2,
                config["device_id"],
                bm_config,
            ),
            "initialize session",
        )
        bm_ready = True
        if bm.bm_rank_id() != bm_config.rank_id:
            raise RuntimeError("SDK assigned the wrong participant")
        pool = SharingPool(
            SharingPlan(**config["plan"]),
            config["mode"],
            config["side"],
            config["member"],
            bm,
            numa_node=config["numa_node"],
        )
        pool.join(config["timeout"])
        cohort.exchange("mapped")
        pool.probe(torch, rank)
        cohort.exchange("probed")
        pool.verify_probe(torch)
        if config["mode"] == "shared":
            for follower in (1, 3):
                torch.npu.synchronize()
                event_offset = len(pool.events)
                cohort.exchange(f"rejoin-{follower}/drained")
                if rank == follower:
                    pool.rejoin_follower(config["timeout"])
                cohort.exchange(f"rejoin-{follower}/joined")
                pool.wait_membership(follower, config["timeout"], since=event_offset)
                pool.probe(torch, rank)
                cohort.exchange(f"rejoin-{follower}/probed")
                pool.verify_probe(torch)
        if pool.mode == "independent" or pool.member == 0:
            stage_local_kv(pool, SimpleNamespace(stage_tokens=1024))
        cohort.exchange("staged")
        report["mapping"] = pool.report()
        allocations = cohort.exchange(
            "allocations", bytes=report["mapping"]["local_dram_bytes"]
        )
        if [r["bytes"] for r in allocations] != pool.plan.contributions(pool.mode):
            raise RuntimeError(
                "actual participant contributions do not match the experiment"
            )
        save(output / "report.json", report)
        if not config["smoke"]:
            if not check_numa_placement(report["mapping"]["numa"]):
                print(
                    "NUMA_UNVERIFIED: davinci_manager mapping exposes no per-node "
                    "page counts; collecting measurements with NUMA acceptance pending",
                    flush=True,
                )
        if not config["smoke"]:
            torch.distributed.init_process_group(
                backend="hccl",
                init_method=f"tcp://{local_host}:{ports['collective']}",
                world_size=2,
                rank=config["member"],
                timeout=datetime.timedelta(seconds=config["timeout"]),
            )
            distributed_ready = True
        report["samples"] = benchmark(pool, cohort, config, torch, output)
        if not config["smoke"] and not config.get("diagnostic", False):
            report["delayed_publication"] = delayed_publication(
                pool, cohort, config, torch, output
            )
        success = True
    except BaseException as exc:
        report.update(error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        if mf_ready:
            report["sdk_error"] = str(mf.get_last_err_msg())
    finally:
        try:
            drained = False
            if torch is not None:
                torch.npu.synchronize()
                drained = True
            if cohort is not None:
                # On any worker failure the coordinator aborts outstanding phases,
                # then accepts only this final four-worker drain handshake.
                outcomes = cohort.exchange("drained", drained=drained, success=success)
                success = success and all(outcome["success"] for outcome in outcomes)
            if distributed_ready:
                torch.distributed.destroy_process_group()
            if pool is not None:
                pool.close(drain=torch.npu.synchronize)
                report["closed"] = True
                report["group_events"] = pool.events
            if bm_ready:
                mf.bm.uninitialize()
            if mf_ready:
                mf.uninitialize()
        except BaseException as exc:
            success = False
            report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            if cohort is not None:
                cohort.close()
            report["status"] = (
                "smoke_passed"
                if success and config["smoke"]
                else "checks_passed_numa_unverified"
                if success and not report["mapping"]["numa"]["placement_verified"]
                else "checks_passed"
                if success
                else "failed"
            )
            save(output / "report.json", report)
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def stop(signum: int, frame: Any) -> None:
        raise RuntimeError(f"worker received signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    config = json.loads(args.config.read_text())
    diagnostic = config.get("diagnostic", False)
    if diagnostic:
        faulthandler.enable()
        checkpoint(args.output, "worker-start")
    try:
        return run(config, args.output)
    finally:
        if diagnostic:
            faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
