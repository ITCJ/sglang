"""Paired TP1/2/4 bulk writes and top-k=2048 reads across seven NUMA layouts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cd_progress import CDProgress
from cd_results import admit, local_groups, save, unit
from cd_resume import compatible_code
from run_numa_sharing import code_identity, completion_outcome, percentile
from run_ticket10 import connect, receive_log, send_log
from ticket10_processes import Peer, Processes

from ascend_mempool.cd_plan import (
    BLOCK_DIM,
    DIM,
    SLOTS,
    TOKENS,
    TOPK,
    Topology,
    cases,
    cpu_partitions,
    schedule,
    topologies,
)
from ascend_mempool.sharing import port_plan
from ascend_mempool.sharing_control import CohortServer
from ascend_mempool.sharing_pool import cpu_list

SCRIPTS = Path(__file__).resolve().parent


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"))
    parser.add_argument("--prefill-host", default="10.120.72.31")
    parser.add_argument("--decode-host", default="10.120.72.32")
    parser.add_argument("--run-id")
    parser.add_argument("--devices", nargs=4, type=int, default=[0, 1, 2, 3])
    parser.add_argument("--numa-nodes", nargs=4, type=int, default=[0, 2, 4, 6])
    parser.add_argument("--cpu-node", type=int, default=0)
    parser.add_argument(
        "--read-sources",
        nargs="+",
        choices=("local", "remote", "mixed"),
        default=["local", "remote", "mixed"],
    )
    parser.add_argument("--contiguous-control", action="store_true")
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--warmup", type=int)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--control-port", type=int, default=19400)
    parser.add_argument(
        "--log-root", type=Path, default=Path("/home/cryang/log/numa-cd")
    )
    parser.add_argument(
        "--resume-from",
        help="Prior run ID; exact measurement, code and environment required",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--smoke", action="store_true")
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    for key, full, smoke in (
        ("rounds", 3, 1),
        ("samples", 20, 2),
        ("repeats", 64, 64),
        ("warmup", 5, 1),
    ):
        if getattr(args, key) is None:
            setattr(args, key, smoke if args.smoke else full)
    try:
        if not args.describe and (args.role is None or args.run_id is None):
            raise ValueError("--role and --run-id are required for NPU runs")
        for run_id in (args.run_id, args.resume_from):
            if run_id is not None and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id
            ):
                raise ValueError("run IDs must be safe directory names")
        if args.resume_from and (
            args.resume_from == args.run_id or args.preflight_only
        ):
            raise ValueError("resume needs a new run ID and a measurement run")
        if len(set(args.devices)) != 4 or any(n < 0 for n in args.devices):
            raise ValueError("need four distinct nonnegative NPU IDs")
        topologies(args.numa_nodes)
        port_plan(args.control_port, "independent", 1, 3, 4)
        if args.cpu_node < 0 or len(set(args.read_sources)) != len(args.read_sources):
            raise ValueError("invalid CPU node or duplicate read source")
        if min(
            args.rounds, args.samples, args.repeats, args.warmup
        ) < 1 or not 0 < args.timeout < float("inf"):
            raise ValueError(
                "rounds, samples, repeats, warmup and timeout must be positive"
            )
    except ValueError as exc:
        parser.error(str(exc))
    return args


def measurement(args: argparse.Namespace) -> dict[str, Any]:
    return dict(
        protocol="numa-cd-v1",
        engine="graph",
        block_dim=BLOCK_DIM,
        tokens=TOKENS,
        slots=SLOTS,
        dim=DIM,
        layers=1,
        dtype="bf16",
        topk=TOPK,
        sources=args.read_sources,
        contiguous=args.contiguous_control,
        seed=args.seed,
        rounds=args.rounds,
        samples=args.samples,
        repeats=args.repeats,
        warmup=args.warmup,
        smoke=args.smoke,
        preflight_only=args.preflight_only,
        devices=args.devices,
        numa_nodes=args.numa_nodes,
        cpu_node=args.cpu_node,
        prefill_host=args.prefill_host,
        decode_host=args.decode_host,
    )


def matrix_cases(args: argparse.Namespace, n: int) -> list[Any]:
    return cases(
        n, args.read_sources, smoke=args.smoke, contiguous=args.contiguous_control
    )


def describe(args: argparse.Namespace) -> dict[str, Any]:
    layouts = topologies(args.numa_nodes)
    order = [] if args.preflight_only else schedule(args.numa_nodes, args.rounds)
    return dict(
        measurement=measurement(args),
        scope="cd_preflight"
        if args.preflight_only
        else "cd_smoke"
        if args.smoke
        else "cd_component",
        layouts=[
            dict(
                **t.signature(),
                plan=asdict(t.plan()),
                contributions=t.plan().contributions(t.mode),
                cases=[c.signature() for c in matrix_cases(args, t.tp_size)],
            )
            for t in layouts
        ],
        order=[dict(round=r, topology=t.name) for r, t in order],
        preflight_groups=len(layouts),
        measured_cases_both_sides=sum(
            2 * len(matrix_cases(args, t.tp_size)) for _, t in order
        ),
        timing="group host envelope / replays; device times reported separately; batch-average percentiles",
        cpu_policy="fixed node; four disjoint CPU subsets; TP1/2 use the same prefix as TP4",
    )


def contract(
    args: argparse.Namespace,
    code: dict[str, str],
    topology: Topology,
    round_id: int,
    member: int,
    cpus: list[list[int]],
) -> dict[str, Any]:
    return dict(
        measurement=measurement(args),
        code=code,
        topology=topology.signature(),
        round=round_id,
        side=0 if args.role == "prefill" else 1,
        member=member,
        device_id=args.devices[member],
        numa_node=topology.nodes[member],
        cpu_affinity=cpus[member],
        samples=args.samples,
        repeats=args.repeats,
        warmup=args.warmup,
    )


def resume_units(
    source: Optional[Path],
    contracts: list[dict[str, Any]],
    args: argparse.Namespace,
    n: int,
) -> list[str]:
    """Only complete, checked cases from every local rank may be proposed for reuse."""
    if source is None:
        return []
    reusable = []
    for case in matrix_cases(args, n):
        for side in (0, 1):
            name = unit(case, side)
            records = []
            for member, expected in enumerate(contracts):
                path = source / f"member-{member}/cases/{name}.json"
                if path.is_file():
                    record = json.loads(path.read_text())
                    admit(record, expected, case, side, member)
                    records.append(record)
            if len(records) == n:
                local_groups(records)
                reusable.append(name)
    return reusable


def summarize(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in groups:
        buckets.setdefault(
            (row["topology"], row["case"]["name"], row["side"]), []
        ).append(row)
    result = []
    for (topology, name, side), rows in buckets.items():
        n = rows[0]["tp_size"]
        item = dict(
            topology=topology,
            case=name,
            side=side,
            tp_size=n,
            batches=len(rows),
            numa_verified=all(r["numa_verified"] for r in rows),
            copy_bytes_per_call=rows[0]["copy_bytes"] // rows[0]["repeats"],
            logical_bytes_per_call=rows[0]["logical_bytes"] // rows[0]["repeats"],
            round_p50_ms={
                str(i): percentile(
                    [r["group_ms"] for r in rows if r["round"] == i], 0.5
                )
                for i in sorted({r["round"] for r in rows})
            },
        )
        for key in ("group_ms", "copy_payload_gbps", "logical_gbps", "launch_skew_ms"):
            for label, fraction in (("p50", 0.5), ("p95", 0.95)):
                item[f"{key}_{label}"] = percentile([r[key] for r in rows], fraction)
        item["device_per_call_ms"] = [
            dict(
                member=m,
                p50=percentile([r["device_per_call_ms"][m] for r in rows], 0.5),
                p95=percentile([r["device_per_call_ms"][m] for r in rows], 0.95),
            )
            for m in range(n)
        ]
        result.append(item)
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "side",
        "topology",
        "case",
        "tp_size",
        "batches",
        "group_ms_p50",
        "group_ms_p95",
        "copy_bytes_per_call",
        "logical_bytes_per_call",
        "copy_payload_gbps_p50",
        "logical_gbps_p50",
        "launch_skew_ms_p95",
        "numa_verified",
    ]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_group(
    args: argparse.Namespace,
    peer: Peer,
    root: Path,
    topology: Topology,
    round_id: int,
    identity: dict[str, Any],
    cpus: list[list[int]],
    progress: CDProgress,
    source_root: Optional[Path],
    *,
    preflight: bool,
) -> list[dict[str, Any]]:
    phase = (
        f"preflight/{topology.name}"
        if preflight
        else f"round-{round_id}/{topology.name}"
    )
    stage = root / phase
    stage.mkdir(parents=True)
    n = topology.tp_size
    contracts = [
        contract(args, identity["code"], topology, round_id, m, cpus) for m in range(n)
    ]
    source = source_root / phase if source_root is not None and not preflight else None
    proposed = resume_units(source, contracts, args, n)
    peer.send("reuse", phase=phase, units=proposed)
    remote = peer.expect("reuse")
    if remote["phase"] != phase or not isinstance(remote["units"], list):
        raise RuntimeError("peer resume phase differs")
    reuse = sorted(set(proposed) & set(remote["units"]))
    save(
        stage / "resume.json", dict(source=str(source) if source else None, units=reuse)
    )
    progress.begin(phase, stage, n, 1 if preflight else 2 * len(matrix_cases(args, n)))
    server = None
    try:
        if args.role == "prefill":
            server = CohortServer(
                args.prefill_host,
                args.control_port + 1,
                f"{args.run_id}/{phase}",
                args.timeout,
                n,
            )
        peer.barrier(phase + "/coordinator")
        started = time.monotonic()
        with Processes(stage) as processes:
            children = []
            for member in range(n):
                config = dict(
                    plan=asdict(topology.plan()),
                    mode=topology.mode,
                    side=contracts[member]["side"],
                    member=member,
                    device_id=args.devices[member],
                    numa_node=topology.nodes[member],
                    prefill_host=args.prefill_host,
                    decode_host=args.decode_host,
                    control_port=args.control_port,
                    timeout=args.timeout,
                    identity=f"{args.run_id}/{phase}",
                    round=round_id,
                    smoke=args.smoke,
                    samples=args.samples,
                    repeats=args.repeats,
                    warmup=args.warmup,
                    cd_matrix=True,
                    cd_preflight=preflight,
                    cd_contract=contracts[member],
                    cd_reuse=reuse,
                    cd_resume_root=str(source / f"member-{member}") if source else None,
                )
                config_path = stage / f"member-{member}.json"
                save(config_path, config)
                children.append(
                    processes.start(
                        f"member-{member}",
                        [
                            sys.executable,
                            "-u",
                            str(SCRIPTS / "sharing_worker.py"),
                            "--config",
                            str(config_path),
                            "--output",
                            str(stage / f"member-{member}"),
                        ],
                    )
                )

            def check() -> None:
                progress.update()
                peer.check()
                if time.monotonic() - started > args.timeout:
                    raise TimeoutError(f"{phase} exceeded {args.timeout}s")
                for child in children:
                    if child.poll() not in (None, 0):
                        raise RuntimeError(
                            f"worker {child.pid} failed with {child.returncode}; see {stage}"
                        )

            for member, child in enumerate(children):
                processes.wait(
                    f"member-{member}", child, timeout=args.timeout, check=check
                )
        reports = [
            json.loads((stage / f"member-{m}/report.json").read_text())
            for m in range(n)
        ]
        for member, report in enumerate(reports):
            if report.get("status") not in (
                "checks_passed",
                "checks_passed_numa_unverified",
                "smoke_passed",
            ) or not report.get("closed"):
                raise RuntimeError(
                    f"{phase} member-{member} did not check and close cleanly"
                )
            if report["config"]["cd_contract"] != contracts[member]:
                raise ValueError("worker returned a different C/D contract")
        peer.barrier(phase + "/closed")
        progress.update(finished=True)
        print(f"PASS {phase} (reused {len(reuse)} cases)", flush=True)
        return reports
    finally:
        if server is not None:
            server.close()


def execute(args: argparse.Namespace, peer: Peer, root: Path) -> dict[str, Any]:
    plan = describe(args)
    identity = dict(
        run_id=args.run_id, plan=plan, code=code_identity(), resume=args.resume_from
    )
    peer.send("identity", value=identity)
    if peer.expect("identity")["value"] != identity:
        raise ValueError("P/D run ID, code, matrix or resume source differs")
    save(root / "identity.json", identity)
    online = cpu_list(Path("/sys/devices/system/node/online").read_text())
    if not set(args.numa_nodes + [args.cpu_node]) <= online:
        raise ValueError(f"requested NUMA/CPU node is offline; online={sorted(online)}")
    cpus = cpu_partitions(
        cpu_list(
            Path(f"/sys/devices/system/node/node{args.cpu_node}/cpulist").read_text()
        )
        & os.sched_getaffinity(0)
    )
    save(
        root / "bindings.json",
        dict(devices=args.devices, cpu_node=args.cpu_node, cpu_partitions=cpus),
    )
    source_root = (
        args.log_root / args.resume_from / args.role if args.resume_from else None
    )
    if source_root is not None:
        previous = json.loads((source_root / "identity.json").read_text())
        if previous["plan"] != plan or not compatible_code(
            previous["code"], identity["code"]
        ):
            raise ValueError(
                "resume requires identical or audited-compatible C/D code and identical measurement configuration"
            )
    progress = CDProgress(
        root, plan["preflight_groups"] + plan["measured_cases_both_sides"]
    )
    allocations = []
    all_groups = []
    local_numa = True
    # All seven mappings must work before any long performance run starts.
    phases = [(-1, t, True) for t in topologies(args.numa_nodes)]
    if not args.preflight_only:
        phases += [(r, t, False) for r, t in schedule(args.numa_nodes, args.rounds)]
    for round_id, topology, preflight in phases:
        reports = run_group(
            args,
            peer,
            root,
            topology,
            round_id,
            identity,
            cpus,
            progress,
            source_root,
            preflight=preflight,
        )
        for report in reports:
            allocations.append(
                dict(
                    topology=topology.name,
                    round=round_id,
                    preflight=preflight,
                    **report["mapping"],
                )
            )
            local_numa &= (
                report["mapping"]["numa"]["placement_verified"]
                and report["cases_numa_verified"]
            )
        if not preflight:
            stage = root / f"round-{round_id}/{topology.name}"
            groups = []
            for case in matrix_cases(args, topology.tp_size):
                for side in (0, 1):
                    records = []
                    for member in range(topology.tp_size):
                        record = json.loads(
                            (
                                stage / f"member-{member}/cases/{unit(case, side)}.json"
                            ).read_text()
                        )
                        admit(
                            record,
                            contract(
                                args, identity["code"], topology, round_id, member, cpus
                            ),
                            case,
                            side,
                            member,
                        )
                        records.append(record)
                    groups.extend(local_groups(records))
            save(stage / "groups.json", groups)
            all_groups.extend(groups)
        save(
            root / "partial-summary.json",
            dict(
                status="incomplete", groups=all_groups, comparison=summarize(all_groups)
            ),
        )
    peer.send("numa-result", verified=local_numa)
    remote_numa = peer.expect("numa-result")["verified"]
    if type(remote_numa) is not bool:
        raise ValueError("invalid peer placement result")
    result = dict(
        **completion_outcome(args.smoke, local_numa, remote_numa),
        scope=plan["scope"],
        role=args.role,
        identity=identity,
        physical_allocations=allocations,
        groups=all_groups,
        comparison=summarize(all_groups),
        user_acceptance="pending",
        ticket_state="open",
        service_performance_acceptance="not run",
    )
    if args.preflight_only:
        result["status"] = (
            "preflight_passed"
            if local_numa and remote_numa
            else "preflight_pending_numa"
        )
    save(root / "summary.json", result)
    write_csv(root / "results.csv", result["comparison"])
    if args.role == "decode":
        send_log(peer, root / "summary.json")
    else:
        receive_log(peer, root / "decode-summary.json")
        remote_result = json.loads((root / "decode-summary.json").read_text())
        write_csv(
            root / "combined-results.csv",
            result["comparison"] + remote_result["comparison"],
        )
    peer.barrier("reports-collected")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.describe:
        print(json.dumps(describe(args), indent=2))
        return 0
    if not args.log_root.resolve().is_relative_to(Path("/home/cryang/log")):
        raise ValueError("NPU logs must be under /home/cryang/log/")
    root = args.log_root / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    save(root / "command.json", sys.argv if argv is None else list(argv))

    def stop(signum: int, frame: Any) -> None:
        raise RuntimeError(f"runner received signal {signum}")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        with Peer(connect(args), args.timeout) as peer:
            try:
                result = execute(args, peer, root)
            except BaseException as exc:
                try:
                    peer.send("error", message=str(exc))
                except OSError:
                    pass
                raise
        print(f"{result['status'].upper()}: {root / 'summary.json'}", flush=True)
        return int(result["exit_code"])
    except BaseException as exc:
        save(
            root / "failure.json",
            dict(status="failed", error=f"{type(exc).__name__}: {exc}"),
        )
        print(f"FAILED: {exc}; logs: {root}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
