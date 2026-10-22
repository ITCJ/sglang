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
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sharing_resume import check_resume_environment
from sharing_startup import wait_for_release
from verify_graph import load_runtime, stage_local_kv

from ascend_mempool.sharing import SharingPlan, configured_engines, port_plan
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
    engines = configured_engines(config)
    for blocks in config["block_dims"]:
        for case, consumer_side in product(
            cases(
                config["smoke"], config.get("diagnose_write_read", False), pool.tp_size
            ),
            (0, 1),
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
    for engine in configured_engines(config):
        case = Case(
            "delayed-writer-publication", "current_parallel", active=pool.tp_size
        )
        work = CopyWorkload(
            pool, case, topk=config["topk"], block_dim=config["block_dims"][0]
        )
        pipeline = Pipeline(
            work,
            torch,
            delay_matmuls=config["delay_matmuls"],
            publication=True,
            publication_timing=engine == "eager",
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
            print(f"BEGIN delay/{engine}/{variant}", flush=True)
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
            timing = {}
            torch.npu.synchronize()
            pipeline.verify()
            if engine == "eager":
                if not hasattr(pipeline.ready, "recorded_time"):
                    raise RuntimeError(
                        "publication diagnostic requires torch_npu Event.recorded_time()"
                    )
                # Same-device timestamps use microseconds since device startup.
                # elapsed_time's start/end contract requires the same stream.
                ready_us, selected_us, writer_us = (
                    event.recorded_time()
                    for event in (
                        pipeline.ready,
                        pipeline.selected_ready,
                        pipeline.writer_done,
                    )
                )
                if (
                    any(
                        type(v) is not int or v < 0
                        for v in (ready_us, selected_us, writer_us)
                    )
                    or selected_us < ready_us
                    or writer_us < ready_us
                ):
                    raise AssertionError(
                        "invalid delayed-publication device timestamps"
                    )
                selected_ms = (selected_us - ready_us) / 1000
                writer_ms = (writer_us - ready_us) / 1000
                gap_ms = (writer_us - selected_us) / 1000
                early = gap_ms > 0
                timing = dict(
                    ready_timestamp_us=ready_us,
                    selected_timestamp_us=selected_us,
                    writer_timestamp_us=writer_us,
                    selected_ms=selected_ms,
                    writer_ms=writer_ms,
                    writer_minus_selected_ms=gap_ms,
                )
            results.append(
                dict(
                    engine=engine,
                    variant=variant,
                    selected_before_writer_done=early,
                    publication_readback_passed=True,
                    fixture_submission_order="selected before delayed writer; no selected-to-writer device fence",
                    graph_early_order_evidence="separate trace"
                    if engine == "graph"
                    else "device timestamps relative to common ready event",
                    **timing,
                )
            )
            save(output / "delayed-publication.json", results)
            if engine == "eager" and pool.member == pool.tp_size - 1 and not early:
                raise AssertionError(
                    "delay fixture has no positive device overlap window: "
                    f"{timing}; inspect stream ordering or increase --delay-matmuls"
                )
            cohort.exchange(f"delay/{engine}/{variant}/done")
            print(f"PASS delay/{engine}/{variant} {timing}", flush=True)
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
        print(f"PASS delay/{engine}/profile", flush=True)
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
        if config.get("cd_matrix"):
            affinity = set(config["cd_contract"]["cpu_affinity"])
            if not affinity <= os.sched_getaffinity(0):
                raise RuntimeError("fixed C/D CPU affinity is unavailable")
        else:
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
        runtime_args = argparse.Namespace(device_id=config["device_id"])
        torch, mf, environment = load_runtime(runtime_args)
        report["environment"] = dict(
            environment, kernel=platform.release(), cpu_affinity=sorted(affinity)
        )
        if config.get("cd_matrix"):
            torch.set_num_threads(min(4, len(affinity)))
            report["environment"].update(
                torch_cpu_threads=torch.get_num_threads(),
                device_visibility={
                    name: os.environ.get(name)
                    for name in (
                        "ASCEND_RT_VISIBLE_DEVICES",
                        "ASCEND_VISIBLE_DEVICES",
                    )
                },
            )
        for path in (
            "/usr/local/Ascend/driver/version.info",
            "/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
        ):
            file = Path(path)
            report["environment"][path] = (
                file.read_text() if file.is_file() else "unavailable"
            )
        check_resume_environment(
            report["environment"], config.get("resume_environment")
        )
        plan = SharingPlan(**config["plan"])
        rank = config["side"] * plan.tp_size + config["member"]
        ports = port_plan(
            config["control_port"],
            config["mode"],
            config["side"],
            config["member"],
            plan.tp_size,
        )
        report["ports"] = ports
        cohort = Cohort(
            config["prefill_host"],
            ports["coordinator"],
            rank,
            config["identity"],
            config["timeout"],
            plan.tp_size,
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
                2 * plan.tp_size if config["mode"] == "shared" else 2,
                config["device_id"],
                bm_config,
            ),
            "initialize session",
        )
        bm_ready = True
        if bm.bm_rank_id() != bm_config.rank_id:
            raise RuntimeError("SDK assigned the wrong participant")
        pool = SharingPool(
            plan,
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
        if (
            config["mode"] == "shared"
            and not config.get("startup_only")
            and (not config.get("cd_matrix") or config.get("cd_preflight"))
        ):
            for follower in range(2 * plan.tp_size):
                if follower % plan.tp_size == 0:
                    continue
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
        if config.get("cd_matrix"):
            # Fail incorrect placement before staging the large fixed KV pool.
            report["mapping"] = pool.report()
            save(output / "report.json", report)
            check_numa_placement(report["mapping"]["numa"])
        if (
            not config.get("cd_preflight")
            and not config.get("startup_only")
            and (pool.mode == "independent" or pool.member == 0)
        ):
            stage_local_kv(pool, argparse.Namespace(stage_tokens=1024))
        cohort.exchange("staged")
        report["mapping"] = pool.report()
        allocations = cohort.exchange(
            "allocations",
            bytes=report["mapping"]["local_dram_bytes"],
            numa_verified=report["mapping"]["numa"]["placement_verified"],
        )
        if [r["bytes"] for r in allocations] != pool.plan.contributions(pool.mode):
            raise RuntimeError(
                "actual participant contributions do not match the experiment"
            )
        save(output / "report.json", report)
        if not config["smoke"] or config.get("cd_matrix") or config.get("startup_only"):
            if not check_numa_placement(report["mapping"]["numa"]):
                print(
                    "NUMA_UNVERIFIED: davinci_manager mapping exposes no per-node "
                    "page counts; collecting measurements with NUMA acceptance pending",
                    flush=True,
                )
        if not config["smoke"] and not config.get("cd_matrix"):
            torch.distributed.init_process_group(
                backend="hccl",
                init_method=f"tcp://{local_host}:{ports['collective']}",
                world_size=plan.tp_size,
                rank=config["member"],
                timeout=datetime.timedelta(seconds=config["timeout"]),
            )
            distributed_ready = True
        if config.get("startup_only"):
            torch.npu.synchronize()
            wait_for_release(output, Path(config["startup_release"]), config["timeout"])
            cohort.exchange("all-pools-live")
            report["startup"] = dict(
                probes_verified=True, all_pools_live=True, participants=pool.world_size
            )
        elif config.get("cd_matrix"):
            from cd_benchmark import benchmark as cd_benchmark

            config["cd_environment"] = report["environment"]
            config["cd_numa_verified"] = all(r["numa_verified"] for r in allocations)
            report["samples"] = cd_benchmark(pool, cohort, config, torch, output)
            report["cases_numa_verified"] = config.get(
                "cd_cases_numa_verified", config["cd_numa_verified"]
            )
        else:
            report["samples"] = (
                []
                if config.get("diagnose_publication", False)
                else benchmark(pool, cohort, config, torch, output)
            )
        if (
            not config["smoke"]
            and not config.get("cd_matrix")
            and (
                not config.get("diagnostic", False)
                or config.get("diagnose_publication", False)
            )
        ):
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

            def cleanup_progress(phase: str) -> None:
                report["cleanup_phase"] = phase
                save(output / "report.json", report)
                print(f"CLEANUP {phase}", flush=True)

            def drain() -> None:
                if torch is not None:
                    torch.npu.synchronize()

            def close_pool() -> None:
                if distributed_ready:
                    torch.distributed.destroy_process_group()
                if pool is not None:
                    pool.close(drain=drain)
                    report["closed"] = True
                    report["group_events"] = pool.events

            def uninitialize() -> None:
                if bm_ready:
                    mf.bm.uninitialize()
                if mf_ready:
                    mf.uninitialize()

            if cohort is not None:
                success = cohort.shutdown(
                    drain=drain,
                    close_pool=close_pool,
                    uninitialize=uninitialize,
                    # Independent pools each have a P-side Store. Shared mode
                    # has only the global rank-0 Store on prefill member 0.
                    store_owner=config["side"] == 0
                    and (config["mode"] == "independent" or config["member"] == 0),
                    success=success,
                    progress=cleanup_progress,
                )
            else:
                drain()
                close_pool()
                uninitialize()
        except BaseException as exc:
            success = False
            report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            if cohort is not None:
                cohort.close()
            report["status"] = (
                "startup_passed"
                if success and config.get("startup_only")
                else "smoke_passed"
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
