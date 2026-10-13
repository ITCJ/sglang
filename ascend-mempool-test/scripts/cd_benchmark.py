"""Graph-only C/D measurement and per-case resumable correctness boundaries."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cd_results import admit, load_reused, save, unit

from ascend_mempool.cd_copy import CDWorkload
from ascend_mempool.cd_plan import CDCase, cases, expected_bytes
from ascend_mempool.sharing import group_sample


def benchmark(
    pool: Any, cohort: Any, config: dict[str, Any], torch: Any, output: Path
) -> list[dict[str, Any]]:
    if config.get("cd_preflight"):
        return []
    stream = torch.npu.Stream()
    results = []
    contract = config["cd_contract"]
    seed = contract["measurement"]["seed"]
    config["cd_cases_numa_verified"] = config["cd_numa_verified"]

    def verify(work: Any, invoke: Any, label: str, variant: int) -> None:
        if work is not None:
            work.prepare(variant)
            work.reset()
        torch.npu.synchronize()
        cohort.exchange(label + "/reset")
        if work is not None:
            with torch.npu.stream(stream):
                invoke()
            stream.synchronize()
        cohort.exchange(label + "/completed")
        if work is not None:
            work.verify()
        torch.npu.synchronize()
        cohort.exchange(label + "/verified")

    # Tail coverage uses the same contiguous partition and pointer path as bulk.
    for side in (0, 1):
        tail = (
            CDWorkload(pool, CDCase("tail-2049", "logical", rows=2049), seed=seed)
            if pool.rank == side
            else None
        )
        for variant in (0, 1):
            verify(
                tail, tail.launch if tail else None, f"tail/{side}/{variant}", variant
            )
        if tail is not None:
            tail.reset()
        torch.npu.synchronize()
        cohort.exchange(f"tail/{side}/restored")
        del tail

    matrix = cases(
        pool.tp_size,
        contract["measurement"]["sources"],
        smoke=config["smoke"],
        contiguous=contract["measurement"]["contiguous"],
    )
    for case in matrix:
        for side in (0, 1):
            name = unit(case, side)
            dest = output / "cases" / (name + ".json")
            print(f"BEGIN {name}", flush=True)
            if name in config["cd_reuse"]:
                source = Path(config["cd_resume_root"]) / "cases" / (name + ".json")
                record = load_reused(
                    source, contract, case, side, pool.member, config["cd_environment"]
                )
                save(dest, record)
                config["cd_cases_numa_verified"] &= record["numa_verified"]
                results.extend(record["samples"])
                cohort.exchange(name + "/reused")
                print(f"PASS {name} REUSED", flush=True)
                continue
            active = pool.rank == side
            work = CDWorkload(pool, case, seed=seed) if active else None
            graph = None
            if work is not None:
                torch.npu.synchronize()
                with torch.npu.stream(stream):
                    for _ in range(config["warmup"]):
                        work.launch()
                stream.synchronize()
                graph = torch.npu.NPUGraph()
                with torch.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
                    work.launch()
                stream.synchronize()
            invoke = graph.replay if graph is not None else None
            verify(work, invoke, name + "/precheck", 0)
            samples = []
            for sample in range(config["samples"]):
                variant = contract["round"] * config["samples"] + sample + 2
                if work is not None:
                    work.prepare(variant)
                torch.npu.synchronize()
                for attempt in range(4):
                    start_event = (
                        torch.npu.Event(enable_timing=True) if active else None
                    )
                    end_event = torch.npu.Event(enable_timing=True) if active else None
                    cohort.aligned_start(f"{name}/sample-{sample}/attempt-{attempt}")
                    row = None
                    if active:
                        assert start_event is not None and end_event is not None
                        assert invoke is not None
                        start_ns = time.monotonic_ns()
                        with torch.npu.stream(stream):
                            start_event.record(stream)
                            for _ in range(config["repeats"]):
                                invoke()
                            end_event.record(stream)
                        stream.synchronize()
                        done_ns = time.monotonic_ns()
                        payload, logical = expected_bytes(
                            case, pool.tp_size, pool.mode, pool.member
                        )
                        row = dict(
                            member=pool.member,
                            side=side,
                            sample=sample,
                            attempt=attempt,
                            start_ns=start_ns,
                            done_ns=done_ns,
                            device_ms=float(start_event.elapsed_time(end_event)),
                            repeats=config["repeats"],
                            copy_bytes=payload,
                            logical_bytes=logical,
                            opposite_side_idle=True,
                        )
                    peers = cohort.exchange(
                        f"{name}/sample-{sample}/attempt-{attempt}/done", row=row
                    )
                    rows = [
                        r["row"]
                        for r in peers[side * pool.tp_size : (side + 1) * pool.tp_size]
                    ]
                    accepted = group_sample(rows, pool.tp_size)["aligned_batches"]
                    if row is not None:
                        row["accepted"] = accepted
                        with (output / "attempts.jsonl").open("a") as stream_log:
                            stream_log.write(json.dumps(dict(unit=name, **row)) + "\n")
                    if accepted:
                        if row is not None:
                            samples.append(row)
                        break
                else:
                    raise RuntimeError(
                        f"{name}: launch skew invalid after 3 retries; inspect attempts.jsonl"
                    )
            # Last sample's output must reflect the new contents and random indices.
            if work is not None:
                work.verify()
            torch.npu.synchronize()
            cohort.exchange(name + "/postcheck")
            if work is not None:
                work.reset()
            torch.npu.synchronize()
            cohort.exchange(name + "/restored")
            record = dict(
                status="checked",
                contract=contract,
                case=case.signature(),
                consumer_side=side,
                samples=samples,
                environment=config["cd_environment"],
                numa_verified=config["cd_numa_verified"],
            )
            admit(record, contract, case, side, pool.member)
            save(dest, record)
            cohort.exchange(name + "/checkpointed")
            results.extend(samples)
            print(f"PASS {name}", flush=True)
            del work, graph, invoke
    return results
