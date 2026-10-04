"""Two-machine BM + UniDexCopy correctness gate, independent of SGLang services."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import socket
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ascend_mempool.control import TestChannel
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.verification import (
    SENTINEL,
    CopyCase,
    expected_output,
    kv_pattern,
    make_cases,
)

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.copy import (
        SparseCopyInputs,
        SparseKVCopy,
    )
    from sglang.srt.hardware_backend.npu.mempool.layout import KVLayout, PoolLayout
else:
    from ascend_mempool.copy import SparseCopyInputs, SparseKVCopy
    from ascend_mempool.layout import KVLayout, PoolLayout

PROTOCOL_VERSION = 1
TARGET_MEMFABRIC_VERSION = "1.1.4"


def parse_args(
    argv: Optional[Sequence[str]] = None, *, default_topk: int = 64
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank", type=int, choices=(0, 1))
    parser.add_argument("--head-ip")
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--nic-url", default="tcp://127.0.0.1:10005")
    parser.add_argument("--store-port", type=int, default=18573)
    parser.add_argument("--control-port", type=int, default=18574)
    parser.add_argument("--pool-id", type=int, default=0)
    parser.add_argument("--layers", type=int, default=2)
    parser.add_argument("--slots", type=int, default=16)
    parser.add_argument("--s-p", type=int, default=16384)
    parser.add_argument("--s-d", type=int, default=16384)
    parser.add_argument("--heads", type=int, default=1)
    parser.add_argument("--kv-dim", type=int, default=576)
    parser.add_argument("--graph-rows", type=int, default=16)
    parser.add_argument("--active-rows", type=int, default=3)
    parser.add_argument("--topk", type=int, default=default_topk)
    parser.add_argument("--block-dims", type=int, nargs="+", default=[24, 48])
    parser.add_argument("--replay-cycles", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--stage-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--log-level", type=int, default=2)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print layout without importing NPU libraries",
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Check local NPU/SDK/operator APIs without opening a pool",
    )
    args = parser.parse_args(argv)
    if (
        not args.describe
        and not args.check_env
        and (args.rank is None or not args.head_ip)
    ):
        parser.error("--rank and --head-ip are required for the paired test")
    if args.slots != 16 or args.graph_rows < 16 or args.graph_rows % 16:
        parser.error(
            "this gate requires 16 physical slots and graph rows in multiples of 16"
        )
    if not 1 <= args.active_rows <= 16:
        parser.error("--active-rows must be in [1, 16]")
    if args.topk < 8 or args.kv_dim < 8:
        parser.error(
            "--topk and --kv-dim must be at least 8 for boundary/payload coverage"
        )
    for name in ("replay_cycles", "warmup", "stage_tokens"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if not args.block_dims or any(value <= 0 for value in args.block_dims):
        parser.error("--block-dims must contain positive core counts")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if not (1 <= args.store_port <= 65535 and 1 <= args.control_port <= 65535):
        parser.error("store/control ports must be in [1, 65535]")
    if args.store_port == args.control_port:
        parser.error("store and test control ports must differ")
    if args.device_id < 0 or args.pool_id < 0:
        parser.error("device and pool IDs must be nonnegative")
    return args


def make_layout(args: argparse.Namespace) -> PoolLayout:
    common = dict(
        layers=args.layers, slots=args.slots, heads=args.heads, dim=args.kv_dim
    )
    layout = PoolLayout(
        KVLayout(tokens=args.s_p, **common), KVLayout(tokens=args.s_d, **common)
    )
    if args.graph_rows * args.topk * layout.prompt.row_bytes > (1 << 32) - 1:
        raise ValueError("destination span exceeds UniDexCopy UINT32_MAX")
    return layout


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def load_runtime(args: argparse.Namespace) -> tuple[Any, Any, dict[str, Any]]:
    torch = importlib.import_module("torch")
    torch_npu = importlib.import_module("torch_npu")
    torch.npu.set_device(args.device_id)
    mf = importlib.import_module("memfabric_hybrid")
    importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
    for name in ("create2", "BmConfig", "BigMemory"):
        if not hasattr(mf.bm, name):
            raise RuntimeError(
                f"MemFabric {TARGET_MEMFABRIC_VERSION} API missing: bm.{name}"
            )
    for name in (
        "peer_rank_ptr",
        "gva_to_va",
        "local_mem_size",
        "copy_data",
        "join",
        "leave",
    ):
        if not hasattr(mf.bm.BigMemory, name):
            raise RuntimeError(f"BM handle API missing: {name}")
    schema = str(torch.ops.npu.unidex_copy.default._schema)
    if "src_ptr" not in schema or "src_rows" not in schema:
        raise RuntimeError(
            "installed UniDexCopy must support raw pointers and explicit row extents"
        )
    environment = {
        "target_memfabric": TARGET_MEMFABRIC_VERSION,
        "memfabric": package_version("memfabric_hybrid"),
        "torch": str(torch.__version__),
        "torch_npu": str(torch_npu.__version__),
        "sgl_kernel_npu": package_version("sgl-kernel-npu"),
        "unidex_schema": schema,
        "device": str(torch.npu.get_device_name(args.device_id)),
        "hostname": socket.gethostname(),
    }
    return torch, mf, environment


def open_channel(args: argparse.Namespace) -> TestChannel:
    if args.rank == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", args.control_port))
            listener.listen(1)
            listener.settimeout(args.timeout)
            print(f"[P] test listener ready on port {args.control_port}", flush=True)
            connection, _ = listener.accept()
    else:
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                connection = socket.create_connection(
                    (args.head_ip, args.control_port),
                    timeout=min(2, max(0.1, deadline - time.monotonic())),
                )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("P test listener did not become available")
                time.sleep(0.2)
    connection.settimeout(args.timeout)
    return TestChannel(connection)


def handshake(
    channel: TestChannel,
    args: argparse.Namespace,
    layout: PoolLayout,
    environment: dict[str, Any],
) -> str:
    configuration = {
        "protocol": PROTOCOL_VERSION,
        "layout": layout.signature(),
        "pool_id": args.pool_id,
        "graph_rows": args.graph_rows,
        "active_rows": args.active_rows,
        "topk": args.topk,
        "block_dims": args.block_dims,
        "replay_cycles": args.replay_cycles,
        "runtime": {
            name: environment[name]
            for name in ("memfabric", "torch", "torch_npu", "sgl_kernel_npu")
        },
    }
    run_id = str(uuid.uuid4()) if args.rank == 0 else ""
    channel.send("HELLO", rank=args.rank, configuration=configuration, run_id=run_id)
    peer = channel.expect("HELLO")
    if peer.get("rank") != 1 - args.rank or peer.get("configuration") != configuration:
        raise RuntimeError(f"peer role/configuration mismatch: {peer}")
    channel.send("CONFIG_OK")
    channel.expect("CONFIG_OK")
    return run_id if args.rank == 0 else str(peer["run_id"])


def stage_local_kv(manager: MempoolKVManager, args: argparse.Namespace) -> None:
    layout = manager.layout.layout_for_rank(manager.rank)
    for layer in range(layout.layers):
        view = manager.view(manager.rank, layer)
        for slot in range(layout.slots):
            for token in range(0, layout.tokens, args.stage_tokens):
                count = min(args.stage_tokens, layout.tokens - token)
                values = kv_pattern(
                    manager.rank, layer, slot, token, count, layout.heads, layout.dim
                ).to("npu")
                view.write_rows(slot, token, values)
        print(
            f"[rank {manager.rank}] STAGED layer={layer} bytes={layout.layer_bytes}",
            flush=True,
        )


def verify_outputs(
    copies: list[SparseKVCopy], case: CopyCase, layout: PoolLayout
) -> int:
    import torch

    verified = 0
    for layer, copy in enumerate(copies):
        actual = copy.output.cpu()
        expected = expected_output(case, layout, layer)
        if not torch.equal(actual, expected):
            first = actual.ne(expected).nonzero()[0].tolist()
            index = tuple(first)
            raise AssertionError(
                f"{case.name} layer={layer} index={index}: "
                f"actual={actual[index].item()} expected={expected[index].item()}"
            )
        verified += actual.numel()
    return verified


def run_copy_checks(
    manager: MempoolKVManager, args: argparse.Namespace, torch: Any
) -> list[dict[str, Any]]:
    results = []
    inputs = SparseCopyInputs(args.graph_rows, args.topk, "npu")
    for blocks in args.block_dims:
        copies = [
            SparseKVCopy(
                manager.view(0, layer), manager.view(1, layer), inputs, block_dim=blocks
            )
            for layer in range(manager.layout.prompt.layers)
        ]

        def launch() -> None:
            for copy in copies:
                copy.output.fill_(SENTINEL)
                copy.gather()

        def check(case: CopyCase, mode: str, cycle: int, start: float) -> None:
            torch.npu.synchronize()
            latency_us = (time.perf_counter() - start) * 1e6
            verified = verify_outputs(copies, case, manager.layout)
            results.append(
                dict(
                    mode=mode,
                    case=case.name,
                    cycle=cycle,
                    block_dim=blocks,
                    verified_elements=verified,
                    latency_us=latency_us,
                )
            )
            print(
                f"[D] PASS {mode} blocks={blocks} cycle={cycle} case={case.name} elements={verified}",
                flush=True,
            )

        torch.npu.synchronize()
        stream = torch.npu.Stream()
        with torch.npu.stream(stream):
            cases = make_cases(
                manager.layout, args.graph_rows, args.topk, args.active_rows
            )
            for case in cases:
                case.load(inputs)
                start = time.perf_counter()
                launch()
                check(case, "eager", 0, start)
            capture_case = cases[0]
            capture_case.load(inputs)
            for _ in range(args.warmup):
                launch()
            torch.npu.synchronize()
            graph = torch.npu.NPUGraph()
            with torch.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
                launch()
            start = time.perf_counter()
            graph.replay()
            check(capture_case, "capture_replay", 0, start)
            for cycle in range(1, args.replay_cycles + 1):
                for case in make_cases(
                    manager.layout, args.graph_rows, args.topk, args.active_rows, cycle
                ):
                    case.load(inputs)
                    start = time.perf_counter()
                    graph.replay()
                    check(case, "replay", cycle, start)
            torch.npu.synchronize()
            del graph
    torch.npu.synchronize()
    return results


def write_report(args: argparse.Namespace, report: dict[str, Any]) -> None:
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def retain_failed_pool(
    args: argparse.Namespace,
    report: dict[str, Any],
    reason: str,
    *,
    bilateral_reads: bool = False,
) -> None:
    """Retain storage while its peer may still submit remote reads."""
    shutdown = (
        "Both peers may read remotely; Ctrl+C will keep storage retained. "
        "Confirm both peers have stopped reading before terminating the test processes."
        if bilateral_reads
        else "Stop D before stopping P."
    )
    print(
        f"[rank {args.rank}] DRAIN_UNCONFIRMED: {reason}; retaining BM pool. {shutdown}",
        flush=True,
    )
    report["retained_pool"] = True
    write_report(args, report)
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            if not bilateral_reads:
                print("[test] operator requested shutdown of retained pool", flush=True)
                return
            print(
                "[test] peer drain remains unconfirmed; BM pool stays retained",
                flush=True,
            )


def run(
    args: argparse.Namespace,
    layout: PoolLayout,
    *,
    stage: Callable[[MempoolKVManager, argparse.Namespace], None] = stage_local_kv,
    paired_checks: Optional[
        Callable[
            [MempoolKVManager, argparse.Namespace, Any, TestChannel],
            list[dict[str, Any]],
        ]
    ] = None,
) -> None:
    """Own paired setup/teardown; optional checks reuse this proven test lifecycle."""
    torch, mf, environment = load_runtime(args)
    if args.check_env:
        print(json.dumps(environment, indent=2))
        return
    report: dict[str, Any] = dict(
        rank=args.rank,
        environment=environment,
        layout=layout.signature(),
        status="running",
    )
    channel = None
    manager = None
    mf_initialized = bm_initialized = False
    may_close_pool = True
    try:
        channel = open_channel(args)
        report["run_id"] = handshake(channel, args, layout, environment)
        mf.set_log_level(args.log_level)
        ret = mf.initialize()
        if ret != 0:
            raise RuntimeError(f"mf.initialize failed: {ret}")
        mf_initialized = True
        config = mf.bm.BmConfig()
        config.auto_ranking = False
        config.rank_id = args.rank
        config.start_store = args.rank == 0
        config.init_timeout = config.create_timeout = config.operation_timeout = (
            math.ceil(args.timeout)
        )
        config.set_nic(args.nic_url)
        ret = mf.bm.initialize(
            f"tcp://{args.head_ip}:{args.store_port}", 2, args.device_id, config
        )
        if ret != 0:
            raise RuntimeError(f"bm.initialize failed: {ret}")
        bm_initialized = True
        # Independent gates use the device index as their simulated TP rank.
        manager = MempoolKVManager.create(
            layout, args.rank, args.pool_id, tp_rank=args.device_id
        )
        manager.join(args.timeout)
        report["mappings"] = [
            dict(
                rank=rank,
                layer=layer,
                gva=f"{manager.view(rank, layer).gva_base:#x}",
                device_va=f"{manager.view(rank, layer).device_base:#x}",
            )
            for rank in (0, 1)
            for layer in range(layout.prompt.layers)
        ]
        print(f"[rank {args.rank}] MAPPED {json.dumps(report['mappings'])}", flush=True)
        channel.send("MAPPED")
        channel.expect("MAPPED")
        marker = torch.full(
            (layout.probe_bytes,), args.rank + 1, dtype=torch.uint8, device="npu"
        )
        manager.probe_peer(marker)
        channel.send("PROBED")
        channel.expect("PROBED")
        stage(manager, args)
        # A writer cannot tear down after it has permitted peer reads.
        if args.rank == 0 or paired_checks is not None:
            may_close_pool = False
        channel.send("DATA_READY")
        channel.expect("DATA_READY")
        if args.rank == 1:
            copy_error = None
            may_close_pool = False
            try:
                report["checks"] = (
                    run_copy_checks(manager, args, torch)
                    if paired_checks is None
                    else paired_checks(manager, args, torch, channel)
                )
            except Exception as exc:
                copy_error = exc
                report["error"] = f"{type(exc).__name__}: {exc}"
            torch.npu.synchronize()
            may_close_pool = paired_checks is None or copy_error is None
            channel.send(
                "DRAINED",
                success=copy_error is None,
                error=report.get("error"),
                checks=len(report.get("checks", [])),
            )
            channel.expect("P_RELEASED")
            may_close_pool = True
            manager.close(drain=torch.npu.synchronize)
            manager = None
            channel.send("D_CLOSED")
            if copy_error is not None:
                raise copy_error
        else:
            if paired_checks is not None:
                report["checks"] = paired_checks(manager, args, torch, channel)
            outcome = channel.expect("DRAINED")
            may_close_pool = True
            report["decode_result"] = outcome
            manager.close(drain=torch.npu.synchronize)
            manager = None
            channel.send("P_RELEASED")
            channel.expect("D_CLOSED")
            if outcome.get("success") is not True:
                raise RuntimeError(f"D verification failed: {outcome}")
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        if mf_initialized:
            report["memfabric_error"] = str(mf.get_last_err_msg())
        if channel is not None:
            try:
                channel.send("ERROR", error=report["error"])
            except OSError:
                pass
        raise
    finally:
        try:
            try:
                if manager is not None:
                    if not may_close_pool:
                        retain_failed_pool(
                            args,
                            report,
                            "peer completion or local NPU drain was not confirmed",
                            bilateral_reads=paired_checks is not None,
                        )
                    try:
                        manager.close(drain=torch.npu.synchronize)
                    except Exception as exc:
                        report["status"] = "failed"
                        report["cleanup_error"] = str(exc)
                        retain_failed_pool(
                            args,
                            report,
                            f"pool close failed: {exc}",
                            bilateral_reads=paired_checks is not None,
                        )
                        raise
                if bm_initialized:
                    mf.bm.uninitialize()
                if mf_initialized:
                    mf.uninitialize()
            finally:
                if channel is not None:
                    channel.close()
        except BaseException as exc:
            report["status"] = "failed"
            report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            write_report(args, report)
    print(f"[rank {args.rank}] ALL_CHECKS_PASSED", flush=True)


def main() -> int:
    try:
        args = parse_args()
        layout = make_layout(args)
        if args.describe:
            print(json.dumps(layout.signature(), indent=2))
        else:
            run(args, layout)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
