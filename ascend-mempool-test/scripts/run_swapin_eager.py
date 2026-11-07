"""One paired eager swap-in topology; B=1/16, both variants, 100 iterations."""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from run_numa_sharing import code_identity
from run_ticket10 import connect, receive_log, send_log, write_json
from ticket10_processes import Peer, Processes
from ascend_mempool.sharing_control import CohortServer
from ascend_mempool.swapin_experiment import (
    BASELINE,
    ITERATIONS,
    SOURCE_ROWS,
    pool_plan,
    source_counts,
    summarize,
)

REPO = Path(__file__).resolve().parents[2]
NPU = "python/sglang/srt/hardware_backend/npu/mempool/"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", choices=("prefill", "decode"), required=True)
    p.add_argument("--prefill-host", default="10.120.72.31")
    p.add_argument("--decode-host", default="10.120.72.32")
    p.add_argument("--tp-size", type=int, choices=(1, 2, 16), required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--log-root", type=Path, default=Path("/home/cryang/log/k2"))
    p.add_argument("--control-port", type=int, default=22900)
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--describe", action="store_true")
    args = p.parse_args(argv)
    if Path(args.run_id).name != args.run_id or args.run_id in ("", ".", ".."):
        p.error("run-id must be a fresh directory name")
    return args


def describe(args):
    plans = [
        pool_plan(args.tp_size, 16, i, host, args.control_port, args.run_id)
        for i, host in enumerate((args.prefill_host, args.decode_host))
    ]
    plans[0].validate_peer(plans[1])
    return dict(
        run_id=args.run_id,
        tp_size=args.tp_size,
        batches=[1, 16],
        hosts=[args.prefill_host, args.decode_host],
        port=args.control_port,
        layers_stride=78,
        measured_layer=0,
        topk=2048,
        row_bytes=1152,
        block_dims=dict(HBM_cache=24, current=48, P_alive=48, D_alive=48, multibase=48),
        iterations=ITERATIONS,
        selection_seed=26029,
        source_rows=SOURCE_ROWS,
        realized_rows_per_request=source_counts(2048),
        numa_nodes=list(range(8)) if args.tp_size == 16 else [0],
        bytes_per_host=[
            sum(p.session(r).contribution for r in range(args.tp_size)) for p in plans
        ],
        baseline=BASELINE,
        baseline_eager_adapter="current src_index.contiguous before copy-start; cost included in metadata and total",
        graph=False,
        observe=False,
        trace=False,
        timing="eager event brackets include host dispatch gaps; not profiler kernel-exclusive time",
        metadata="per-call lookup/routes/masks/task updates rebuilt; immutable base tables initialized separately",
        order="B1 baseline100/materialize100 then B16 baseline100/materialize100; no copy warmup",
        scope="synthetic source proportions on real BM; not replay of original locality or model-service acceptance",
    )


def snapshot(root):
    directory = root / "baseline"
    directory.mkdir()
    hashes = {}
    for name in ("copy", "runtime"):
        data = subprocess.check_output(
            ["git", "show", f"{BASELINE}:{NPU}{name}.py"], cwd=REPO
        )
        (directory / (name + ".py")).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    # These baseline dependencies must remain byte-identical for an honest path.
    for name in ("decode_write", "indexed_copy", "manager"):
        old = subprocess.check_output(
            ["git", "show", f"{BASELINE}:{NPU}{name}.py"], cwd=REPO
        )
        if old != (REPO / NPU / (name + ".py")).read_bytes():
            raise ValueError(f"baseline dependency changed: {name}")
    return directory, hashes


def run(args):
    root = args.log_root / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    side = int(args.role == "decode")
    server = None
    report = dict(status="failed", cleanup="not-started", user_accepted=False)
    try:
        directory, hashes = snapshot(root)
        identity = dict(
            plan=describe(args), code=code_identity(), baseline_sha256=hashes
        )
        write_json(root / "identity.json", identity)
        with Peer(connect(args), args.timeout) as peer:
            try:
                peer.send("identity", value=identity)
                if peer.expect("identity")["value"] != identity:
                    raise ValueError("P/D code or experiment configuration differs")
                if not side:
                    server = CohortServer(
                        args.prefill_host,
                        args.control_port + 1,
                        args.run_id,
                        args.timeout,
                        args.tp_size,
                    )
                peer.barrier("coordinator-ready")
                jobs = Processes(root)
                report["cleanup"] = "pending"
                try:
                    children = []
                    for rank in range(args.tp_size):
                        cfg = dict(
                            tp=args.tp_size,
                            side=side,
                            rank=rank,
                            hosts=identity["plan"]["hosts"],
                            port=args.control_port,
                            run_id=args.run_id,
                            timeout=args.timeout,
                            baseline_dir=str(directory),
                            output=str(root / f"rank-{rank}"),
                        )
                        path = root / f"worker-{rank}.json"
                        write_json(path, cfg)
                        children.append(
                            jobs.start(
                                f"rank-{rank}",
                                [
                                    sys.executable,
                                    "-u",
                                    str(Path(__file__).with_name("swapin_worker.py")),
                                    "--config",
                                    str(path),
                                ],
                            )
                        )
                    deadline = time.monotonic() + args.timeout
                    while not all(
                        (root / f"rank-{rank}" / "ready").exists()
                        for rank in range(args.tp_size)
                    ):
                        peer.check()
                        for rank, child in enumerate(children):
                            jobs.alive(f"rank-{rank}", child)
                        if time.monotonic() > deadline:
                            raise TimeoutError("swap-in workers timed out")
                        time.sleep(0.1)
                    peer.barrier("both-measured")
                    for rank in range(args.tp_size):
                        other = root / "peer" / f"rank-{rank}"
                        other.mkdir(parents=True)
                        for name in ("result.json", "mapping.json", "environment.json"):
                            source = root / f"rank-{rank}" / name
                            if side:
                                receive_log(peer, other / name)
                                send_log(peer, source)
                            else:
                                send_log(peer, source)
                                receive_log(peer, other / name)
                    peer.barrier("collected")
                finally:
                    try:
                        jobs.stop()
                        report["cleanup"] = "passed"
                    except Exception:
                        report["cleanup"] = "failed"
                        raise
                peer.barrier("owned-workers-stopped")
                droot = root if side else root / "peer"
                ranks = [
                    json.loads((droot / f"rank-{r}" / "result.json").read_text())
                    for r in range(args.tp_size)
                ]
                report.update(
                    status="measured",
                    plan=identity["plan"],
                    comparison=summarize(ranks),
                )
            except BaseException as error:
                try:
                    peer.send("error", message=str(error))
                except OSError:
                    pass
                raise
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        if server:
            server.close()
        write_json(root / "report.json", report)
    print(
        f"SWAPIN_EAGER_MEASURED report={root / 'report.json'}; stopped for user cleanup confirmation",
        flush=True,
    )


def main(argv=None):
    args = parse_args(argv)
    if args.describe:
        print(json.dumps(describe(args), indent=2))
        return 0

    def interrupted(*_):
        raise RuntimeError("interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        run(args)
        return 0
    except Exception as error:
        print(f"SWAPIN_EAGER_FAILED {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
