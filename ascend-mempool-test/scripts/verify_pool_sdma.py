"""Two-group P/D SDMA demo; no model and no production routing changes."""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", choices=("prefill", "decode"), required=True)
    p.add_argument("--mapping", type=Path, required=True)
    p.add_argument("--prefill-host", required=True)
    p.add_argument("--decode-host", required=True)
    p.add_argument("--port", type=int, default=29100)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--report-dir", type=Path, required=True)
    p.add_argument("--worker", type=int, choices=range(4), help=argparse.SUPPRESS)
    return p.parse_args()


def write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2))
    temporary.replace(path)


def worker(args, report):
    import torch
    import torch_npu  # noqa: F401
    import memfabric_hybrid as mf
    import sgl_kernel_npu.sparsity_driven_kv_offload  # noqa: F401
    from ascend_mempool.layout import KVLayout, PoolLayout
    from ascend_mempool.manager import MempoolKVManager
    from ascend_mempool.sharing_plan import MempoolNumaConfig, HostTopology, RankNuma
    from ascend_mempool.pool_copy import CopyBlock, CopyTask, PoolCopyExecutor
    from ascend_mempool.indexed_copy import copy_rows

    rank = args.worker
    side = int(args.role == "decode")
    mapping = json.loads(args.mapping.read_text())
    rows = mapping[args.role]
    if len(rows) != 4 or [r["rank"] for r in rows] != list(range(4)):
        raise ValueError("mapping requires four ordered ranks per side")
    device = rows[rank]["device"]
    host = (args.prefill_host, args.decode_host)[side]
    torch.npu.set_device(device)
    dist = torch.distributed
    dist.init_process_group(
        "gloo",
        init_method=f"tcp://{args.prefill_host}:{args.port}",
        rank=side * 4 + rank,
        world_size=8,
        timeout=datetime.timedelta(seconds=args.timeout),
    )
    signatures = [None] * 8
    dist.all_gather_object(signatures, mapping)
    if any(s != mapping for s in signatures):
        raise ValueError("P/D mappings differ")
    geometry = lambda layers, slots, tokens, dim: KVLayout(
        layers, slots, tokens, 1, dim, page_size=128
    )
    layout = PoolLayout(
        geometry(78, 2, 1024, 576),
        geometry(78, 2, 128, 576),
        prompt_index=geometry(21, 2, 1024, 128),
        prefix_main=geometry(78, 8, 128, 576),
        prefix_index=geometry(21, 8, 128, 128),
    )
    plan = MempoolNumaConfig(
        rank_map=tuple(RankNuma(host, r["rank"], r["node"]) for r in rows)
    ).plan(
        hosts=(
            HostTopology(
                host,
                tuple(range(4)),
                Path("/sys/devices/system/node/online").read_text().strip(),
            ),
        ),
        tp_size=4,
        role=args.role,
        instance_id="ticket11-demo",
        kv_identity="synthetic-page-first",
        layout=layout,
        base_port=args.port + 1,
        nic_port=args.port + 100,
        prefill_shared=True,
        decode_shared=True,
        prefill_index_shared=True,
        prefix_shared=True,
    )
    if tuple(g.members for g in plan.groups) != ((0, 1), (2, 3)):
        raise ValueError("mapping must contain groups (0,1), (2,3) on two NUMA nodes")
    session = plan.session(rank)
    report["ports"] = [
        dict(host=host, port=session.nic_base_port + session.participant)
    ]
    if side == 0 and rank == 0:
        report["ports"].extend(
            [dict(host=host, port=args.port), dict(host=host, port=session.store_port)]
        )
    manager = MempoolKVManager.initialize_session(
        session=session,
        store_host=args.prefill_host,
        nic_host=host,
        device_id=device,
        timeout=args.timeout,
    )
    report.update(
        role=args.role,
        rank=rank,
        device=device,
        numa=session.numa_node,
        mf_module=mf.__file__,
        mf_version=getattr(mf, "__version__", "unknown"),
        torch_version=torch.__version__,
        torch_npu_version=torch_npu.__version__,
        pid=os.getpid(),
        cases=[],
        status="running",
        user_accepted=False,
    )

    def setup():
        torch.npu.set_device(device)
        raw = (
            Path(f"/sys/devices/system/node/node{session.numa_node}/cpulist")
            .read_text()
            .strip()
        )
        cpus = set()
        for part in raw.split(","):
            ends = list(map(int, part.split("-")))
            cpus.update(range(ends[0], ends[-1] + 1))
        cpus &= os.sched_getaffinity(0)
        os.sched_setaffinity(0, cpus)
        report["executor_cpus"] = sorted(os.sched_getaffinity(0))

    executor = PoolCopyExecutor(
        manager.pool_transfer(), timeout=args.timeout, setup=setup
    )

    # Patterns are built independently as [layer, token, feature] for each page.
    def pattern(region, page, group=0):
        layers, dim = (78, 576) if region == "main" else (21, 128)
        return (
            (
                torch.arange(
                    layers * 128 * dim, device=f"npu:{device}", dtype=torch.int32
                )
                + page * 17
                + (group * 31 if region == "main" else 0)
                + (53 if region == "index" else 0)
            )
            % 127
        ).to(torch.bfloat16)

    def view(region, segment=None):
        return manager.view(0, 0, region=region, segment=segment)

    def block(region, page, segment=None):
        return view(region, segment).page_block(page)

    def put(region, page, value, segment=None):
        torch.npu.current_stream().synchronize()
        gva, _, size = block(region, page, segment)
        ret = manager._handle.copy_data(
            value.data_ptr(), gva, size, mf.bm.BmCopyType.L2G
        )
        if ret:
            raise RuntimeError(f"initialization copy returned {ret}")

    def check(region, page, expected_page, segment=None):
        # Real device-indexed reader, separate from BM batch/wait under test.
        base = view(region, segment)
        layers, dim = base.layout.layers, base.layout.dim
        output = torch.empty(
            (layers, 128, 1, dim), dtype=torch.bfloat16, device=f"npu:{device}"
        )
        logical = torch.arange(
            page * 128, (page + 1) * 128, dtype=torch.int64, device=f"npu:{device}"
        )
        target = torch.arange(128, dtype=torch.int64, device=f"npu:{device}")
        for layer in range(layers):
            src = manager.view(0, layer, region=region, segment=segment)
            copy_rows(
                torch.ops.npu.unidex_copy,
                torch.empty(1, dtype=torch.bfloat16),
                output[layer],
                logical,
                target,
                torch.ones_like(target, dtype=torch.bool),
                row_bytes=src.layout.row_bytes,
                source_view=src,
            )
        torch.npu.synchronize()
        if not torch.equal(
            output.flatten().cpu(),
            pattern(
                "index" if "index" in region else "main",
                expected_page,
                expected_page % 2,
            ).cpu(),
        ):
            raise AssertionError(
                f"device read mismatch {region} page {page} segment {segment}"
            )

    if side == 0:
        for region in ("main", "index"):
            if rank % 2 == 0 and (region == "main" or rank == 0):
                for page in range(8):
                    put("alive_" + region, page, pattern(region, page, rank // 2))
            if rank % 2 == 0:
                for page in range(4):
                    put(
                        "prefix_" + region,
                        page,
                        torch.full_like(pattern(region, page), -1),
                        rank // 2,
                    )
    dist.barrier()
    idle_ticks = []
    idle_end = time.monotonic() + 0.05
    while time.monotonic() < idle_end:
        idle_ticks.append(time.monotonic())
        time.sleep(0.0001)
    report["idle_ticks"] = idle_ticks
    for op_id, kind in enumerate(("admit", "fill"), 1):
        if kind == "fill" and side == 0 and rank % 2 == 0:
            for region in ("main", "index"):
                if region == "index" and rank != 0:
                    continue
                for page in range(8):
                    put(
                        "alive_" + region,
                        page,
                        torch.full_like(pattern(region, page), -1),
                    )
        dist.barrier()
        blocks, paths = [], []
        if side == 0:
            for page in range(8):
                segment, local = page % 2, page // 2
                for region in ("main", "index"):
                    owns = (
                        rank == segment * 2
                        if kind == "admit"
                        else (
                            (page // 4 == rank % 2) if region == "main" else rank == 0
                        )
                    )
                    if not owns:
                        continue
                    alive = block("alive_" + region, page)
                    prefix = block("prefix_" + region, local, segment)
                    src, dst = (alive, prefix) if kind == "admit" else (prefix, alive)
                    blocks.append(CopyBlock(src[0], dst[0], src[2]))
                    paths.append(
                        dict(
                            region=region,
                            page=page,
                            segment=segment,
                            src_gva=src[0],
                            dst_gva=dst[0],
                            bytes=src[2],
                        )
                    )
        report["active_task"] = dict(
            rid="synthetic", op_id=op_id, kind=kind, paths=paths
        )
        executor.submit(CopyTask("synthetic", op_id, kind, tuple(blocks)))
        ticks = []
        while True:
            ticks.append(time.monotonic())
            result = executor.poll(op_id)
            if result.state != "PENDING":
                break
            time.sleep(0.0001)
        report["active_task"].update(
            state=result.state,
            error=result.error,
            ticks=ticks,
            evidence=result.evidence,
        )
        if result.state != "READY":
            raise RuntimeError(f"{kind}: {result.state}: {result.error}")
        report["cases"].append(
            dict(
                kind=kind,
                state=result.state,
                paths=paths,
                ticks=ticks,
                ticks_during_wait=sum(
                    result.evidence.get("submit_returned", 0)
                    <= t
                    <= result.evidence.get("wait_returned", 0)
                    for t in ticks
                ),
                evidence=result.evidence,
            )
        )
        dist.barrier()
        if kind == "admit" and side == 0:
            for page in range(8):
                for region in ("main", "index"):
                    check("prefix_" + region, page // 2, page, page % 2)
        if kind == "fill":
            # D reads P's alive replica through its imported device mapping.
            for page in range(8):
                for region in ("main", "index"):
                    check("alive_" + region, page, page)
        dist.barrier()
    executor.close()
    report.update(
        status="passed",
        interface_ready=False,
        acceptance="functional-smoke-only",
        content="device-indexed full-page comparison",
        interface_status="unverified: SDK visibility contract and child failure propagation",
        scope="two groups; no production service acceptance",
    )
    report["code_version"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    report["loaded_libraries"] = sorted(
        {
            line.split()[-1]
            for line in Path("/proc/self/maps").read_text().splitlines()
            if "/" in line
            and any(name in line for name in ("memfabric", "libhybm", "libsmem"))
        }
    )
    for case in report["cases"]:
        case["gil_runtime"] = (
            "observed-progress"
            if case["ticks_during_wait"]
            else "unverified-no-samples"
        )
    report["interface_facts"] = dict(
        completion="BM wait, same executor thread; not a per-batch handle",
        all_subtasks="unverified: local GitCode batch may suppress submit errors",
        visibility="device indexed readers tested; hardware fence contract unverified",
        gil="local GitCode binding releases GIL; inspect ticks_during_wait for runtime evidence",
        cross_owner="covered by mixed-owner prefix reads in fill",
        entry_limit="API uint32 count; no smaller documented limit found; demo maximum recorded in cases",
        child_failure="SDK returns scalar status; task/op identity retained, failed block identity unavailable",
        reference_sdk="GitCode 1.3.0 29fbd1ae14e7; installed binary equivalence unverified",
    )
    all_reports = [None] * 8
    dist.all_gather_object(all_reports, report)
    for case, expected_count, expected_bytes in (
        (0, 16, 8 * (11501568 + 688128)),
        (1, 24, 8 * (2 * 11501568 + 688128)),
    ):
        paths = [p for r in all_reports for p in r["cases"][case]["paths"]]
        if (
            len(paths) != expected_count
            or sum(p["bytes"] for p in paths) != expected_bytes
        ):
            raise AssertionError("global batch coverage/bytes mismatch")
    if rank == 0:
        (args.report_dir / "all-ranks.json").write_text(
            json.dumps(all_reports, indent=2)
        )
    dist.destroy_process_group()
    return report


def main():
    args = arguments()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    if args.worker is not None:
        path = args.report_dir / f"rank-{args.worker}.json"
        report = dict(status="running", cases=[])
        try:
            report = worker(args, report)
        except Exception:
            report.update(
                status="failed", interface_ready=False, error=traceback.format_exc()
            )
            write_report(path, report)
            return 1
        write_report(path, report)
        # Parent owns process cleanup, as for service workers. No explicit BM destructor gate.
        while True:
            time.sleep(1)
    if any(args.report_dir.iterdir()):
        raise ValueError("use a fresh report directory")
    processes, logs = [], []
    status = 1
    try:
        for rank in range(4):
            log = (args.report_dir / f"rank-{rank}.log").open("w")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    [sys.executable, __file__, *sys.argv[1:], "--worker", str(rank)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )
        deadline = time.monotonic() + args.timeout * 6
        while time.monotonic() < deadline:
            reports = [args.report_dir / f"rank-{r}.json" for r in range(4)]
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("worker exited; see rank logs")
            if all(p.exists() for p in reports):
                status = (
                    0
                    if all(
                        json.loads(p.read_text())["status"] == "passed" for p in reports
                    )
                    else 1
                )
                break
            time.sleep(0.2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        from verify_mempool_session import released_port

        port_results = []
        for path in args.report_dir.glob("rank-*.json"):
            for endpoint in json.loads(path.read_text()).get("ports", []):
                try:
                    port_results.append(released_port(**endpoint, timeout=args.timeout))
                except Exception as exc:
                    status = 1
                    port_results.append(dict(**endpoint, error=str(exc)))
        (args.report_dir / "launcher.json").write_text(
            json.dumps(
                dict(
                    exit_code=status,
                    ports=port_results,
                    interface_ready=False,
                    children_reaped=all(p.poll() is not None for p in processes),
                    user_accepted=False,
                ),
                indent=2,
            )
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
