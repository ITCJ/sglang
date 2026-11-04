"""Ticket14: paired standalone bandwidth measurements; one command per host."""

from __future__ import annotations

import argparse
import json
import importlib
import math
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from run_numa_sharing import code_identity
from run_ticket10 import connect, receive_log, send_log, write_json
from ticket10_processes import Peer, Processes

from ascend_mempool.bandwidth import BenchmarkConfig, build_plan
from ascend_mempool.bandwidth_device import pool_plan
from ascend_mempool.bandwidth_report import save_report
from ascend_mempool.sharing_control import CohortServer

SCRIPTS = Path(__file__).resolve().parent
ARTIFACTS = (
    "workloads.json",
    "samples.json",
    "pipeline.json",
    "background.json",
    "residency.json",
    "mapping.json",
    "environment.json",
    "status.json",
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", choices=("prefill", "decode"), required=True)
    p.add_argument("--prefill-host", required=True)
    p.add_argument("--decode-host", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--devices", type=int, nargs="+", default=list(range(16)))
    p.add_argument("--numa-nodes", type=int, nargs="+", default=list(range(8)))
    p.add_argument("--page-size", type=int, choices=(64, 128), default=64)
    p.add_argument(
        "--suite", choices=("remote-write", "full", "smoke"), default="remote-write"
    )
    p.add_argument("--iterations", type=int, default=100)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--control-port", type=int, default=21000)
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--log-root", type=Path, default=Path("/home/cryang/log/k2"))
    p.add_argument("--describe", action="store_true")
    p.add_argument("--mooncake-protocol", choices=("rdma", "tcp"), default="rdma")
    p.add_argument("--mooncake-device", default="")
    p.add_argument(
        "--layer-window-ms",
        type=float,
        help="Optional same-machine layer compute window; feasibility only, not overlap proof",
    )
    args = p.parse_args(argv)
    if (
        not args.run_id
        or Path(args.run_id).name != args.run_id
        or args.run_id in (".", "..")
    ):
        p.error("run-id must be a fresh simple directory name")
    if len(args.devices) not in (1, 2, 4, 8, 16) or len(set(args.devices)) != len(
        args.devices
    ):
        p.error("devices must list 1/2/4/8/16 distinct device IDs")
    if (
        not args.numa_nodes
        or len(set(args.numa_nodes)) != len(args.numa_nodes)
        or len(args.devices) % len(args.numa_nodes)
    ):
        p.error("NUMA nodes must be distinct and divide the device count")
    if args.suite == "full" and (len(args.devices) != 16 or len(args.numa_nodes) != 8):
        p.error("full coverage requires 16 devices and eight NUMA nodes")
    if (
        args.iterations < 1
        or args.warmup < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        p.error("iterations, warmup and timeout must be positive")
    if not 1 <= args.control_port <= 64000:
        p.error("control-port must leave room for SDK NIC ports (1..64000)")
    if args.layer_window_ms is not None and (
        not math.isfinite(args.layer_window_ms) or args.layer_window_ms <= 0
    ):
        p.error("layer-window-ms must be positive and finite")
    return args


def configuration(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        page_size=args.page_size,
        tp_size=len(args.devices),
        nodes=tuple(args.numa_nodes),
        iterations=args.iterations,
        warmup=args.warmup,
        suite=args.suite,
    )


def describe(args: argparse.Namespace) -> dict[str, Any]:
    c = configuration(args)
    plans = [
        pool_plan(c, side, host, args.control_port, args.run_id)
        for side, host in enumerate((args.prefill_host, args.decode_host))
    ]
    plans[0].validate_peer(plans[1])
    return dict(
        benchmark=c.signature(),
        hosts=[args.prefill_host, args.decode_host],
        devices=args.devices,
        port=args.control_port,
        run_id=args.run_id,
        cases=[asdict(case) for case in build_plan(c)],
        bytes_per_host=[
            sum(plan.session(rank).contribution for rank in range(c.tp_size))
            for plan in plans
        ],
        storage="production page_first; alive slot1 for readers, slot0 for writers",
        remote_write="one D segment-0 replica; P prefix interleaved; writers partition pages",
        scope="smoke" if args.suite == "smoke" else args.suite,
        timing="eager host submit-to-completion; no device trace",
    )


def worker_command(path: Path) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(SCRIPTS / "bandwidth_worker.py"),
        "--config",
        str(path),
    ]


def run_pair(
    args: argparse.Namespace,
    *,
    worker_command: Callable[[Path], list[str]] = worker_command,
) -> dict[str, Any]:
    root = args.log_root / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    identity = dict(plan=describe(args), code=code_identity())
    write_json(root / "identity.json", identity)
    side = int(args.role == "decode")
    server = None
    result: dict[str, Any] = dict(status="failed", cleanup="pending")
    try:
        with Peer(connect(args), args.timeout) as peer:
            try:
                peer.send("identity", value=identity)
                if peer.expect("identity")["value"] != identity:
                    raise RuntimeError("P/D config, code or run ID differs")
                if side == 0:
                    server = CohortServer(
                        args.prefill_host,
                        args.control_port + 1,
                        args.run_id,
                        args.timeout,
                        len(args.devices),
                    )
                peer.barrier("coordinator-ready")
                processes = Processes(root)
                try:
                    outputs, children = [], []
                    for rank, device in enumerate(args.devices):
                        output = root / f"rank-{rank}"
                        settings = dict(
                            benchmark=configuration(args).signature(),
                            side=side,
                            rank=rank,
                            device=device,
                            output=str(output),
                            hosts=[args.prefill_host, args.decode_host],
                            port=args.control_port,
                            run_id=args.run_id,
                            timeout=args.timeout,
                        )
                        path = root / f"worker-{rank}.json"
                        write_json(path, settings)
                        children.append(
                            processes.start(f"rank-{rank}", worker_command(path))
                        )
                        outputs.append(output)
                    deadline = time.monotonic() + args.timeout
                    while not all((output / "ready").exists() for output in outputs):
                        peer.check()
                        for rank, child in enumerate(children):
                            Processes.alive(f"rank-{rank}", child)
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "bandwidth workers exceeded measurement timeout"
                            )
                        time.sleep(0.1)
                    for output in outputs:
                        if (
                            json.loads((output / "status.json").read_text())["status"]
                            != "passed"
                        ):
                            raise RuntimeError(f"worker did not pass: {output}")
                    peer.barrier("both-measured")
                    for rank, output in enumerate(outputs):
                        other = root / "peer" / f"rank-{rank}"
                        other.mkdir(parents=True)
                        for name in (*ARTIFACTS, "worker.log"):
                            source = (
                                root / f"rank-{rank}.log"
                                if name == "worker.log"
                                else output / name
                            )
                            if side == 0:
                                send_log(peer, source)
                                receive_log(peer, other / name)
                            else:
                                receive_log(peer, other / name)
                                send_log(peer, source)
                    peer.barrier("reports-collected")
                finally:
                    try:
                        processes.stop()
                        result["cleanup"] = "passed"
                    except Exception:
                        result["cleanup"] = "failed"
                        raise
                result.update(status="measured")
                peer.barrier("owned-workers-stopped")
                from bandwidth_mooncake import run_optional

                result["mooncake"] = run_optional(args, peer, root)
            except Exception as error:
                result["status"] = "failed"
                try:
                    peer.send("error", message=str(error))
                except OSError:
                    pass
                raise
    finally:
        if server:
            server.close()
        write_json(root / "runner.json", result)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.describe:
        print(json.dumps(describe(args), indent=2))
        return 0

    def interrupted(signum: int, frame: Any) -> None:
        raise RuntimeError(f"interrupted by signal {signum}")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    root = args.log_root / args.run_id / args.role
    try:
        importlib.import_module(
            "matplotlib"
        )  # Fail before BM allocation if plots cannot be produced
    except ImportError as error:
        print(f"TICKET14_PREFLIGHT_FAILED {error}", file=sys.stderr, flush=True)
        return 1
    try:
        outcome = run_pair(args)
        rows = [
            row
            for path in sorted(root.glob("**/samples.json"))
            for row in json.loads(path.read_text())
        ]
        metadata = dict(
            outcome,
            scope=args.suite,
            config=configuration(args).signature(),
            layer_window_ms=args.layer_window_ms,
            overlap="not_proven_by_transfer_only_microbenchmark",
        )
        save_report(root, rows, metadata)
        from bandwidth_summary import finish_report

        finish_report(root, args)
        print(
            f"TICKET14_MEASUREMENTS_COMPLETE report={root / 'report.json'}", flush=True
        )
        return 0
    except FileExistsError as error:
        print(f"TICKET14_REFUSED_EXISTING_RUN {error}", file=sys.stderr, flush=True)
        return 1
    except Exception as error:
        print(f"TICKET14_FAILED {error}", file=sys.stderr, flush=True)
        if root.exists():
            write_json(root / "failure.json", dict(error=repr(error)))
            write_json(
                root / "report.json",
                dict(status="failed", error=repr(error), user_accepted=False),
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
