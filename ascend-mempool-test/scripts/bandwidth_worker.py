"""One real NPU worker; lifecycle is owned by run_bandwidth.py."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from verify_graph import load_runtime

from ascend_mempool.bandwidth import BenchmarkConfig, Case, build_plan
from ascend_mempool.bandwidth_device import (
    Device,
    ReadWork,
    WriteWork,
    pipeline,
    pool_plan,
)

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.manager import MempoolKVManager
else:
    from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.sharing_control import Cohort
from ascend_mempool.sharing_pool import numa_evidence


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def binary_identity(package: str) -> dict[str, str]:
    module = importlib.import_module(package)
    directory = Path(str(module.__file__)).parent
    result = {}
    for path in sorted(directory.rglob("*.so")):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result[str(path.relative_to(directory))] = digest.hexdigest()
    return result


class Background:
    def __init__(self, work: Any, device: Device, timeout: float) -> None:
        self.work, self.device, self.timeout = work, device, timeout
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.count = 0
        self.window_start = 0
        self.operations: list[tuple[int, int]] = []
        self.start_ns = self.done_ns = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        try:
            self.device.torch.npu.set_device(self.device.device_id)
            self.start_ns = time.monotonic_ns()
            while not self.stop.is_set():
                started = time.monotonic_ns()
                self.work.run()
                self.operations.append((started, time.monotonic_ns()))
                self.count += 1
                self.ready.set()
            self.done_ns = time.monotonic_ns()
        except BaseException as error:
            self.error = error
            self.ready.set()

    def begin(self) -> None:
        self.thread.start()
        if not self.ready.wait(self.timeout):
            raise TimeoutError("background stream never became ready")
        if self.error:
            raise self.error

    def finish(self) -> dict[str, Any]:
        window_end = time.monotonic_ns()
        self.stop.set()
        self.thread.join(self.timeout)
        if self.thread.is_alive():
            raise TimeoutError("background stream did not complete")
        if self.error:
            raise self.error
        selected = [
            (a, b)
            for a, b in self.operations
            if a >= self.window_start and b <= window_end
        ]
        return dict(
            durations_ms=[(b - a) / 1e6 for a, b in selected],
            window_start_ns=self.window_start,
            window_end_ns=window_end,
            window_operations=len(selected),
            window_note="local aligned start to completion barrier; includes control boundary lag",
            operations=self.count,
            bytes_per_op=self.work.nbytes,
            start_ns=self.start_ns,
            done_ns=self.done_ns,
        )


def benchmark(d: Device, cohort: Cohort, root: Path, timeout: float) -> None:
    c, t = d.config, d.torch
    rows, pipes, background_rows, manifests = [], [], [], []
    for case_id, case in enumerate(build_plan(c)):
        print(f"BEGIN {case.name}", flush=True)
        active = d.side == case.side and (
            case.operation == "write" or d.rank < case.readers
        )
        bg_case = None
        if case.background == "read":
            bg_case = Case("d-local-background", "readers", side=1, readers=c.tp_size)
        elif case.background == "write":
            bg_case = Case(
                "sequential-write-background",
                "locations",
                operation="write",
                side=case.side,
                tokens=131072,
            )
        if case.operation == "read":
            d.initialize_read(case)
        if bg_case and bg_case.operation == "read":
            d.initialize_read(bg_case)
        cohort.exchange(case.name + "/initialized")
        for sample in range(c.samples):
            variant = case_id * c.samples + sample + 1
            if case.pipeline:
                cohort.aligned_start(f"{case.name}/{sample}/start")
                if active:
                    pipes.append(
                        dict(rank=d.rank, sample=sample, **pipeline(d, case, variant))
                    )
                cohort.exchange(f"{case.name}/{sample}/written")
                if d.side != case.side:
                    for layer in range(c.layers):
                        WriteWork(d, case, layer, variant).verify()
                cohort.exchange(f"{case.name}/{sample}/verified")
                continue
            work: Any = None
            if case.operation == "write":
                work = WriteWork(d, case, variant=variant)
            elif active:
                work = ReadWork(d, case)
            if active and sample == 0:
                entry = dict(
                    case=case.name, rank=d.rank, side=d.side, segments=work.segments
                )
                if case.operation == "write":
                    entry.update(
                        pages=work.pages,
                        destinations=[dict(gva=b[1], nbytes=b[2]) for b in work.blocks],
                    )
                else:
                    entry.update(
                        source_side=work.side,
                        source_segment=work.segment,
                        byte_offsets=work.offsets,
                        block_bytes=work.size,
                        kernel_tile_bytes=work.tile,
                    )
                manifests.append(entry)
                save(root / "workloads.json", manifests)
            for _ in range(c.warmup if active else 0):
                work.run()
            background = None
            if bg_case and d.side == bg_case.side:
                if bg_case.operation == "read":
                    background = Background(ReadWork(d, bg_case), d, timeout)
                elif d.rank == 0:
                    bg_device = Device(
                        d.pool, t, d.bm, replace(c, tp_size=1), d.side, 0, d.device_id
                    )
                    background = Background(
                        WriteWork(bg_device, bg_case, variant=variant), d, timeout
                    )
            if background:
                background.begin()
            cohort.aligned_start(f"{case.name}/{sample}/start")
            if background:
                background.window_start = time.monotonic_ns()
            if active:
                durations = []
                cpu_start = time.process_time()
                start_ns = time.monotonic_ns()
                first_start = first_done = 0
                for iteration in range(c.iterations):
                    begin = time.monotonic_ns()
                    work.run()
                    end = time.monotonic_ns()
                    if iteration == 0:
                        first_start, first_done = begin, end
                    durations.append((end - begin) / 1e6)
                done_ns = time.monotonic_ns()
                rows.append(
                    dict(
                        case=case.name,
                        rank=d.rank,
                        side=d.side,
                        sample=sample,
                        first_operation_start_ns=first_start,
                        first_operation_done_ns=first_done,
                        start_ns=start_ns,
                        done_ns=done_ns,
                        durations_ms=durations,
                        bytes_per_op=work.nbytes,
                        segments=work.segments,
                        cpu_seconds=time.process_time() - cpu_start,
                    )
                )
            cohort.exchange(f"{case.name}/{sample}/completed")
            if background:
                background_rows.append(
                    dict(
                        case=case.name,
                        sample=sample,
                        rank=d.rank,
                        **background.finish(),
                    )
                )
            if case.operation == "read" and active:
                work.verify()
            elif case.operation == "write" and d.side != case.side:
                work.verify()
            cohort.exchange(f"{case.name}/{sample}/checked")
            save(root / "samples.json", rows)
        save(root / "pipeline.json", pipes)
        save(root / "background.json", background_rows)
        print(f"PASS {case.name}", flush=True)
    # Pipeline-only/empty-active ranks still provide a complete artifact manifest.
    save(root / "workloads.json", manifests)
    save(root / "samples.json", rows)
    save(root / "pipeline.json", pipes)
    save(root / "background.json", background_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text())
    root = Path(settings["output"])
    root.mkdir(parents=True, exist_ok=True)
    try:
        config = BenchmarkConfig(
            **{**settings["benchmark"], "nodes": tuple(settings["benchmark"]["nodes"])}
        )
        side, rank = settings["side"], settings["rank"]
        torch, mf, environment = load_runtime(
            argparse.Namespace(device_id=settings["device"])
        )
        environment.update(
            kernel_binaries=binary_identity("sgl_kernel_npu"),
            sdk_binaries=binary_identity("memfabric_hybrid"),
        )
        environment["numa_topology"] = {
            str(node): {
                name: Path(f"/sys/devices/system/node/node{node}/{name}").read_text()
                for name in ("cpulist", "distance", "meminfo")
            }
            for node in config.nodes
        }
        save(root / "environment.json", environment)
        plan = pool_plan(
            config, side, settings["hosts"][side], settings["port"], settings["run_id"]
        )
        session = plan.session(rank)
        pool = MempoolKVManager.initialize_session(
            session=session,
            store_host=settings["hosts"][0],
            nic_host=settings["hosts"][side],
            device_id=settings["device"],
            timeout=settings["timeout"],
            bm_module=mf.bm,
        )
        cohort = Cohort(
            settings["hosts"][0],
            settings["port"] + 1,
            side * config.tp_size + rank,
            settings["run_id"],
            settings["timeout"],
            config.tp_size,
        )
        d = Device(pool, torch, mf.bm, config, side, rank, settings["device"])
        save(
            root / "mapping.json",
            dict(
                regions=pool.replica_registration(),
                numa_node=session.numa_node,
                affinity=sorted(os.sched_getaffinity(0)),
            ),
        )
        cohort.exchange("mappings-ready")
        benchmark(d, cohort, root, settings["timeout"])
        evidence = []
        if session.contribution:
            gva = pool._handle.peer_rank_ptr(session.participant, mf.bm.BmMemType.HOST)
            address = pool._handle.gva_to_va(gva, mf.bm.BmMemType.LOCAL_HOST)
            evidence.append(numa_evidence(address, session.numa_node))
        save(root / "residency.json", evidence)
        if any(e["status"] == "mismatch" for e in evidence):
            raise RuntimeError(
                "observed physical NUMA placement differs from requested node"
            )
        cohort.exchange("measurements-complete")
        save(root / "status.json", dict(status="passed", rank=rank, side=side))
        (root / "ready").touch()
        # The launcher owns worker termination, including the SDK store owner.
        # Keep mappings/source owners alive until it has both hosts' reports.
        while True:
            time.sleep(1)
    except Exception:
        save(root / "status.json", dict(status="failed", error=traceback.format_exc()))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
