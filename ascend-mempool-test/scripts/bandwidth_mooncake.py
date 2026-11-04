"""Optional paired Mooncake get comparison; only P contributes object storage."""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any

from run_ticket10 import connect, receive_log, send_log, write_json
from ticket10_processes import Peer, Processes
from ascend_mempool.bandwidth_report import percentile


def probe(master: bool) -> dict[str, Any]:
    try:
        importlib.import_module("mooncake.store")
        binary = shutil.which("mooncake_master")
        if master and not binary:
            raise FileNotFoundError("mooncake_master is not on PATH")
        return dict(available=True, master=binary)
    except Exception as error:
        return dict(available=False, reason=repr(error))


def run_optional(args: argparse.Namespace, peer: Peer, root: Path) -> dict[str, Any]:
    if args.suite != "full":
        return dict(
            status="not_requested", reason="small-read comparison belongs to full suite"
        )
    checked = subprocess.run(
        [sys.executable, str(Path(__file__)), "--probe", args.role],
        capture_output=True,
        text=True,
        timeout=30,
    )
    local = (
        json.loads(checked.stdout)
        if checked.returncode == 0
        else dict(available=False, reason=checked.stderr)
    )
    peer.send("mooncake-probe", value=local)
    remote = peer.expect("mooncake-probe")["value"]
    if not local["available"] or not remote["available"]:
        result = dict(status="unavailable", local=local, peer=remote)
        write_json(root / "mooncake.json", result)
        return result
    directory = root / "mooncake"
    directory.mkdir()
    with Processes(directory) as processes:
        if args.role == "prefill":
            child = processes.start(
                "master",
                [
                    local["master"],
                    f"--rpc_port={args.control_port + 4}",
                    f"--rpc_address={args.prefill_host}",
                ],
            )
            deadline = time.monotonic() + 30
            while True:
                if child.poll() is not None or time.monotonic() > deadline:
                    local = dict(
                        available=False,
                        reason="master startup failed; see mooncake/master.log",
                    )
                    break
                try:
                    with socket.create_connection(
                        (args.prefill_host, args.control_port + 4), timeout=1
                    ):
                        break
                except OSError:
                    time.sleep(0.1)
        peer.send("mooncake-master", value=local)
        remote = peer.expect("mooncake-master")["value"]
        if not local["available"] or not remote["available"]:
            result = dict(status="unavailable", local=local, peer=remote)
            write_json(root / "mooncake.json", result)
            return result
        command = [
            sys.executable,
            "-u",
            str(Path(__file__)),
            "--role",
            args.role,
            "--prefill-host",
            args.prefill_host,
            "--decode-host",
            args.decode_host,
            "--control-port",
            str(args.control_port + 3),
            "--timeout",
            str(args.timeout),
            "--run-id",
            args.run_id,
            "--iterations",
            str(args.iterations),
            "--protocol",
            args.mooncake_protocol,
            "--device",
            args.mooncake_device,
            "--output",
            str(directory / "result.json"),
        ]
        child = processes.start("client", command)
        processes.wait("mooncake client", child, timeout=args.timeout, check=peer.check)
        peer.barrier("mooncake-clients-done")
        if args.role == "prefill":
            receive_log(peer, directory / "peer.json")
            send_log(peer, directory / "result.json")
        else:
            send_log(peer, directory / "result.json")
            receive_log(peer, directory / "peer.json")
        result = dict(
            local=json.loads((directory / "result.json").read_text()),
            peer=json.loads((directory / "peer.json").read_text()),
        )
        for endpoint in result.values():
            endpoint["medians"] = {}
            for size in (1152, 4096, 16384, 147456):
                records = [
                    r
                    for r in endpoint.get("measurements", [])
                    if r["slice_bytes"] == size
                ]
                if records:
                    endpoint["medians"][str(size)] = dict(
                        repeats=len(records),
                        **{
                            key: median(r[key] for r in records)
                            for key in (
                                "ops_per_second",
                                "cpu_cores",
                                "get_p99_ms",
                                "step_p99_ms",
                            )
                        },
                    )

            for size, entry in endpoint["medians"].items():
                records = [
                    r for r in endpoint["measurements"] if str(r["slice_bytes"]) == size
                ]
                entry["median_run_step_p99_ms"] = entry["step_p99_ms"]
                entry["median_run_get_p99_ms"] = entry["get_p99_ms"]
                entry["step_p99_ms"] = percentile(
                    [v for r in records for v in r["raw_step_ms"]], 0.99
                )
                entry["get_p99_ms"] = percentile(
                    [v for r in records for v in r["raw_get_ms"]], 0.99
                )
                entry["p99_aggregation"] = (
                    "pooled operation samples from all three runs"
                )
            for measured in endpoint.get("measurements", []):
                measured.pop("raw_get_ms", None)
                measured.pop("raw_step_ms", None)
        result["status"] = (
            "measured"
            if all(v["status"] == "measured" for v in result.values())
            else "unavailable"
        )
        write_json(root / "mooncake.json", result)
        peer.barrier("mooncake-collected")
    return result


def measure(args: argparse.Namespace) -> int:
    store = importlib.import_module("mooncake.store").MooncakeDistributedStore()
    report: dict[str, Any] = dict(
        status="unavailable",
        protocol=args.protocol,
        endpoint="remote object get into CPU bytes; not NPU HBM",
        measurements=[],
    )
    try:
        with Peer(connect(args), args.timeout) as peer:
            local_host = (
                args.prefill_host if args.role == "prefill" else args.decode_host
            )
            setup: dict[str, Any]
            try:
                ret = store.setup(
                    local_host,
                    "P2PHANDSHAKE",
                    512 * 1024 * 1024 if args.role == "prefill" else 0,
                    64 * 1024 * 1024,
                    args.protocol,
                    args.device,
                    f"{args.prefill_host}:{args.control_port + 1}",
                )
                if ret != 0:
                    raise RuntimeError(f"Mooncake setup returned {ret}")
                setup = dict(ok=True)
            except Exception as error:
                setup = dict(ok=False, reason=repr(error))
            peer.send("setup", value=setup)
            remote = peer.expect("setup")["value"]
            if not setup["ok"] or not remote["ok"]:
                report.update(local_setup=setup, peer_setup=remote)
            else:
                report["status"] = "measured"
                for size in (1152, 4096, 16384, 147456):
                    keys = [f"{args.run_id}/{size}/{i}" for i in range(2048)]
                    value = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
                    if args.role == "prefill":
                        for key in keys:
                            if store.put(key, value) != 0:
                                raise RuntimeError("Mooncake put failed")
                    peer.barrier(f"{size}/ready")
                    if args.role == "decode":
                        for key in keys:
                            if store.get(key) != value:
                                raise AssertionError("Mooncake content mismatch")
                        for sample in range(3):
                            steps, operations = [], []
                            cpu = time.process_time()
                            begin = time.perf_counter()
                            for _ in range(args.iterations):
                                started = time.perf_counter()
                                for key in keys:
                                    stamp = time.perf_counter()
                                    data = store.get(key)
                                    operations.append(
                                        (time.perf_counter() - stamp) * 1000
                                    )
                                    if len(data) != size:
                                        raise AssertionError(
                                            "Mooncake get returned wrong length"
                                        )
                                steps.append((time.perf_counter() - started) * 1000)
                            elapsed = time.perf_counter() - begin
                            report["measurements"].append(
                                dict(
                                    slice_bytes=size,
                                    sample=sample,
                                    ops_per_second=len(operations) / elapsed,
                                    cpu_cores=(time.process_time() - cpu) / elapsed,
                                    get_p99_ms=percentile(operations, 0.99),
                                    step_p99_ms=percentile(steps, 0.99),
                                    median_step_ms=median(steps),
                                    get_samples=len(operations),
                                    step_samples=len(steps),
                                    raw_get_ms=operations,
                                    raw_step_ms=steps,
                                )
                            )
                    peer.barrier(f"{size}/read")
                    if args.role == "prefill":
                        for key in keys:
                            store.remove(key)
                peer.barrier("finished")
        write_json(args.output, report)
        return 0
    except Exception as error:
        report.update(status="failed", error=repr(error))
        write_json(args.output, report)
        return 1


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        print(json.dumps(probe(sys.argv[2] == "prefill")))
        return 0
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("role", "prefill-host", "decode-host", "run-id"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--control-port", type=int, required=True)
    p.add_argument("--timeout", type=float, required=True)
    p.add_argument("--iterations", type=int, required=True)
    p.add_argument("--protocol", required=True)
    p.add_argument("--device", default="")
    p.add_argument("--output", type=Path, required=True)
    return measure(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
