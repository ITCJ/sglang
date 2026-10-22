"""Two ranks per host share 96 GiB on each selected NUMA; four NPU BM peers."""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from run_numa_sharing import code_identity
from run_ticket10 import connect, receive_log, send_log, write_json
from sharing_startup import check_reports, wait_for_workers
from ticket10_processes import Peer, Processes

from ascend_mempool.sharing import SharingPlan, port_plan
from ascend_mempool.sharing_control import CohortServer

SCRIPTS = Path(__file__).resolve().parent
GIB = 1 << 30


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--prefill-host", required=True)
    parser.add_argument("--decode-host", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--numa-nodes", type=int, nargs="+", default=list(range(8)))
    parser.add_argument("--control-port", type=int, default=20000)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument(
        "--log-root", type=Path, default=Path("/home/cryang/log/mempool-shared-startup")
    )
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.run_id):
        parser.error("run-id must be a simple, fresh directory name")
    if len(set(args.numa_nodes)) != len(args.numa_nodes) or any(
        node not in range(8) for node in args.numa_nodes
    ):
        parser.error("numa-nodes must be distinct IDs in [0, 8)")
    args.numa_nodes.sort()
    if args.prefill_host == args.decode_host:
        parser.error("P and D must be separate hosts")
    if not math.isfinite(args.timeout) or args.timeout < 1:
        parser.error("timeout must be finite and at least one second")
    try:
        if args.control_port < 1:
            raise ValueError("control-port must be positive")
        for node in args.numa_nodes:
            port_plan(args.control_port + 32 * (node + 1), "shared", 1, 1)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def describe(args: argparse.Namespace) -> dict[str, Any]:
    # Payload + four 64-byte identity markers rounds to exactly 96 GiB/owner.
    plan = SharingPlan(
        prompt_tokens=71680, decode_tokens=71680, slots=16, layers=78, tp_size=2
    )
    contributions = plan.contributions("shared")
    if contributions != [96 * GIB, 0, 96 * GIB, 0]:
        raise RuntimeError("startup layout does not yield exactly 96 GiB per owner")
    return dict(
        scope="shared BM allocation/mapping/probe/teardown smoke",
        prefill_host=args.prefill_host,
        decode_host=args.decode_host,
        control_port=args.control_port,
        plan=asdict(plan),
        contributions=contributions,
        bytes_per_numa=96 * GIB,
        bytes_per_host=len(args.numa_nodes) * 96 * GIB,
        groups=[
            dict(
                numa_node=node,
                devices=[2 * node, 2 * node + 1],
                control_port=args.control_port + 32 * (node + 1),
                participants=4,
            )
            for node in args.numa_nodes
        ],
    )


def execute(args: argparse.Namespace, peer: Peer, root: Path) -> dict[str, Any]:
    plan = describe(args)
    identity = dict(run_id=args.run_id, plan=plan, code=code_identity())
    peer.send("identity", value=identity)
    if peer.expect("identity")["value"] != identity:
        raise RuntimeError("P/D plan, code fingerprint or run ID differs")
    write_json(root / "identity.json", identity)
    side = 0 if args.role == "prefill" else 1
    servers = []
    outputs = []
    try:
        if side == 0:
            for group in plan["groups"]:
                servers.append(
                    CohortServer(
                        args.prefill_host,
                        group["control_port"] + 1,
                        f"{args.run_id}/node-{group['numa_node']}",
                        args.timeout,
                        2,
                    )
                )
        peer.barrier("coordinators-ready")
        with Processes(root) as processes:
            children = []
            for group in plan["groups"]:
                for member, device in enumerate(group["devices"]):
                    output = root / f"device-{device}"
                    outputs.append(output)
                    config = dict(
                        plan=plan["plan"],
                        mode="shared",
                        side=side,
                        member=member,
                        device_id=device,
                        numa_node=group["numa_node"],
                        prefill_host=args.prefill_host,
                        decode_host=args.decode_host,
                        control_port=group["control_port"],
                        timeout=args.timeout,
                        identity=f"{args.run_id}/node-{group['numa_node']}",
                        smoke=True,
                        startup_only=True,
                        startup_release=str(root / "release"),
                    )
                    config_path = root / f"device-{device}.json"
                    write_json(config_path, config)
                    child = processes.start(
                        f"device-{device}",
                        [
                            sys.executable,
                            "-u",
                            str(SCRIPTS / "sharing_worker.py"),
                            "--config",
                            str(config_path),
                            "--output",
                            str(output),
                        ],
                    )
                    children.append((device, child))
            write_json(
                root / "pids.json",
                [dict(device=device, pid=child.pid) for device, child in children],
            )

            def check_children(*, must_be_live: bool = False) -> None:
                peer.check()
                for device, child in children:
                    code = child.poll()
                    if code is not None and (must_be_live or code != 0):
                        raise RuntimeError(f"device {device} exited early: {code}")

            wait_for_workers(
                outputs, args.timeout, lambda: check_children(must_be_live=True)
            )
            # Workers remain blocked on release until every host is fully allocated.
            peer.send("all-pools-live")
            peer.expect(
                "all-pools-live", check=lambda: check_children(must_be_live=True)
            )
            check_children(must_be_live=True)
            (root / "release").touch(exist_ok=False)
            print(
                f"ALL_SHARED_POOLS_LIVE nodes={args.numa_nodes} "
                f"local_GiB={plan['bytes_per_host'] // GIB}",
                flush=True,
            )
            try:
                for device, child in children:
                    processes.wait(
                        f"device-{device}",
                        child,
                        timeout=args.timeout,
                        check=check_children,
                    )
            finally:
                write_json(
                    root / "exits.json",
                    [
                        dict(device=device, exit_code=child.poll())
                        for device, child in children
                    ],
                )
            reports = [json.loads((out / "report.json").read_text()) for out in outputs]
            result = check_reports(reports, plan, side)
            result.update(role=args.role, identity=identity, workers=reports)
            # Keep every P coordinator alive until both hosts finish SDK teardown.
            peer.barrier("workers-closed")
    finally:
        for server in servers:
            server.close()
    peer.send("result", numa_placement_verified=result["numa_placement_verified"])
    remote = peer.expect("result")
    result["both_hosts_numa_placement_verified"] = (
        result["numa_placement_verified"] and remote["numa_placement_verified"]
    )
    write_json(root / "summary.json", result)
    if side == 1:
        send_log(peer, root / "summary.json")
    else:
        receive_log(peer, root / "decode-summary.json")
    peer.barrier("reports-collected")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.describe:
        print(json.dumps(describe(args), indent=2))
        return 0
    if not args.log_root.resolve().is_relative_to(Path("/home/cryang/log")):
        raise ValueError("NPU results must be saved below /home/cryang/log/")
    root = args.log_root.resolve() / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "command.json", sys.argv if argv is None else list(argv))

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
        print(
            "ALL_SHARED_BM_STARTUP_CHECKS_PASSED "
            f"numa_placement_verified={result['both_hosts_numa_placement_verified']} "
            f"report={root / 'summary.json'}",
            flush=True,
        )
        return 0
    except BaseException as exc:
        write_json(root / "failure.json", dict(status="failed", error=str(exc)))
        print(f"FAILED: {exc}; logs: {root}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
