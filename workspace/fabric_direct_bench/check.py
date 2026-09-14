#!/usr/bin/env python3
"""One-page remote Host DRAM -> final NPU L1 check using MemFabric BM."""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import traceback
import threading
from datetime import datetime
from importlib import metadata
from pathlib import Path

# Reuse only the benchmark's data format, validation and quiet logging helpers.
# This does not import or run SGLang or Mooncake.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kv_path_bench"))
from feasibility_check import check_page
from feasibility_log import enable_log, print_result
from kv_layout import K_DIM, LAYERS, PAGE_BYTES, PAGE_SIZE, ROPE_DIM, split_page_payload


STORE_PORT = 19571
CONTROL_PORT = 19573
NIC_PORT = 19575
POOL_BYTES = 1 << 30
PROTOCOL = "a3-bm-host-to-l1-v1"


def transfer_plan(source_gva, k_base, rope_base, count=1, slot=1):
    """Scatter one packed page directly into a slot of layer-first MLA L1."""
    k_layer = PAGE_SIZE * K_DIM * 2
    rope_layer = PAGE_SIZE * ROPE_DIM * 2
    sources, targets, sizes = [], [], []
    for layer in range(LAYERS):
        sources.extend((source_gva + layer * k_layer,
                        source_gva + LAYERS * k_layer + layer * rope_layer))
        # Slot zero is reserved in each layer.
        targets.extend((k_base + (layer * (count + 1) + slot) * k_layer,
                        rope_base + (layer * (count + 1) + slot) * rope_layer))
        sizes.extend((k_layer, rope_layer))
    return sources, targets, sizes


def send(connection, event, **fields):
    connection.sendall((json.dumps({"event": event, **fields}) + "\n").encode())


def receive(reader, expected):
    raw = reader.readline(8193)
    if not raw or len(raw) > 8192 or not raw.endswith(b"\n"):
        raise RuntimeError("peer closed or sent an invalid control message")
    message = json.loads(raw)
    if message.get("event") != expected:
        raise RuntimeError(f"expected {expected}, peer sent {message}")
    return message


def check_rc(value, operation):
    if value != 0:
        raise RuntimeError(f"{operation} returned {value}")


def worker(args):
    role = args.role
    source = role == "source"
    source_ip = args.local_ip if source else args.source_ip
    rank = 0 if source else 1
    pool_bytes = 10 * POOL_BYTES if args.performance else POOL_BYTES
    logfile = args.run_dir / "native.log" if args.run_dir else Path(f"/tmp/a3-fabric-{role}.log")
    enable_log("fabric", role, path=logfile)
    result = {
        "status": "running", "role": role, "page_bytes": PAGE_BYTES,
        "source_memory": "remote BM Host DRAM", "destination_memory": "NPU HBM L1",
        "transport": "BM SDMA", "copy_type": "GH2L", "rank": rank,
        "local_ip": args.local_ip, "source_ip": source_ip, "device": args.device,
        "bm_host_bytes_per_rank": pool_bytes, "bm_hbm_bytes_per_rank": 0,
        "extra_receive_staging": False, "timed": args.performance,
        "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=Path(__file__).parent, capture_output=True,
                                 text=True).stdout.strip(),
    }
    mf = bm = handle = connection = reader = listener = None
    mf_ready = bm_ready = joined = False
    failure = None
    code, stage = "F1", "imports and public API check"
    try:
        import torch
        import torch_npu
        import memfabric_hybrid as mf
        from memfabric_hybrid import bm

        # Fail before taking an NPU if this wheel lacks the required public API.
        for symbol in ("initialize", "uninitialize", "create2", "BmConfig", "BmCopyType", "BmMemType", "BmDataOpType"):
            if not hasattr(bm, symbol):
                raise RuntimeError(f"installed memfabric BM lacks {symbol}")
        for enum, symbol in ((bm.BmCopyType, "H2GH"), (bm.BmCopyType, "GH2L"),
                             (bm.BmMemType, "HOST"), (bm.BmMemType, "DEVICE"),
                             (bm.BmDataOpType, "SDMA")):
            if not hasattr(enum, symbol):
                raise RuntimeError(f"installed BM enum lacks {symbol}")
        config = bm.BmConfig()
        if not hasattr(config, "set_nic"):
            raise RuntimeError("installed BmConfig lacks set_nic")
        result["versions"] = {"torch": torch.__version__, "torch_npu": torch_npu.__version__}
        try:
            result["versions"]["memfabric_hybrid"] = metadata.version("memfabric-hybrid")
        except metadata.PackageNotFoundError:
            result["versions"]["memfabric_hybrid"] = getattr(mf, "__version__", "unknown")

        code, stage = "F2", "control connection"
        if source:
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.local_ip, CONTROL_PORT))
            listener.listen(1)
            listener.settimeout(args.timeout)
            print_result("FR")  # Start the client now; BM may wait for both ranks.
            connection, _ = listener.accept()
        else:
            connection = socket.create_connection((source_ip, CONTROL_PORT), timeout=30)
        connection.settimeout(args.timeout)
        reader = connection.makefile("rb")
        send(connection, "HELLO", protocol=PROTOCOL, rank=rank, performance=args.performance)
        hello = receive(reader, "HELLO")
        if hello.get("protocol") != PROTOCOL or hello.get("rank") != 1 - rank:
            raise RuntimeError("peer protocol or rank mismatch")
        if hello.get("performance", False) != args.performance:
            raise RuntimeError("both ends must use the same performance mode")

        code, stage = "F3", "BM initialize, allocate Host pool and join"
        torch.npu.set_device(args.device)
        mf.set_log_level(1)
        check_rc(mf.initialize(), "mf.initialize")
        mf_ready = True
        config.rank_id = rank
        config.auto_ranking = False
        config.start_store = source
        config.init_timeout = 60
        config.create_timeout = 60
        config.operation_timeout = 60
        config.set_nic(f"tcp://{args.local_ip}:{NIC_PORT}")
        check_rc(bm.initialize(f"tcp://{source_ip}:{STORE_PORT}", 2, args.device, config), "bm.initialize")
        bm_ready = True
        # Follow the public BM DRAM example: both ranks contribute Host memory.
        # Client's contribution is unused; it is never a KV receive staging area.
        handle = bm.create2(id=0, local_dram_size=pool_bytes, max_dram_size=pool_bytes,
                            local_hbm_size=0, max_hbm_size=0,
                            data_op_type=bm.BmDataOpType.SDMA)
        if handle is None:
            raise RuntimeError("bm.create2 returned no handle")
        check_rc(handle.join(), "BM join")
        joined = True
        if handle.local_mem_size(bm.BmMemType.HOST) < PAGE_BYTES * (1024 if args.performance else 1):
            raise RuntimeError("BM Host pool is smaller than the workload")
        if handle.local_mem_size(bm.BmMemType.DEVICE) != 0:
            raise RuntimeError("unexpected HBM contribution: experiment requires a Host source")

        if source:
            code, stage = "F4", "prepare source Host page"
            gva = handle.peer_rank_ptr(0, bm.BmMemType.HOST)
            if not gva:
                raise RuntimeError("source Host GVA is null")
            for page in range(1024 if args.performance else 1):
                payload = bytearray(split_page_payload(page))
                tensor = torch.frombuffer(payload, dtype=torch.uint8)
                check_rc(handle.copy_data(tensor.data_ptr(), gva + page * PAGE_BYTES, PAGE_BYTES, bm.BmCopyType.H2GH, 0), "H2GH fill")
                check_rc(handle.wait(), "source BM wait")
            send(connection, "READY", page_bytes=PAGE_BYTES, memory="HOST")
            print_result("FS")
            code, stage = "F2", "wait for client verification and cleanup"
            receive(reader, "DONE")
        else:
            code, stage = "F4", "wait for source Host data"
            ready = receive(reader, "READY")
            if ready.get("page_bytes") != PAGE_BYTES or ready.get("memory") != "HOST":
                raise RuntimeError("unexpected source layout or memory type")
            source_gva = handle.peer_rank_ptr(0, bm.BmMemType.HOST)
            if not source_gva:
                raise RuntimeError("remote rank 0 Host GVA is null")
            code, stage = "F5", "GH2L directly into final NPU L1"
            if args.performance:
                from performance import run_client
                run_client(handle, bm, torch, source_gva, args)
            else:
                check_one_page(handle, bm, torch, source_gva, result)
    except Exception as exc:
        failure = (code, stage, repr(exc))
        if mf is not None and hasattr(mf, "get_last_err_msg"):
            try:
                result["native_error"] = mf.get_last_err_msg()
            except Exception:
                pass
        print(f"FABRIC_FAIL stage={stage} error={exc!r}", flush=True)
        traceback.print_exc()
    finally:
        actions = []
        if joined:
            actions.append(("leave", lambda: check_rc(handle.leave(), "BM leave")))
        if handle is not None:
            actions.append(("destroy", handle.destroy))
        if bm_ready:
            actions.append(("bm uninitialize", lambda: bm.uninitialize(0)))
        if mf_ready:
            actions.append(("mf uninitialize", mf.uninitialize))
        for name, action in actions:
            try:
                action()
            except Exception as exc:
                failure = failure or ("F9", name, repr(exc))
                traceback.print_exc()
        if connection is not None:
            try:
                if failure:
                    send(connection, "ERROR", code=failure[0], stage=failure[1])
                elif source:
                    send(connection, "ACK")
                else:
                    # Source stays alive until the client has finished BM cleanup.
                    send(connection, "DONE")
                    receive(reader, "ACK")
            except Exception as exc:
                failure = failure or ("F2", "completion handshake", repr(exc))
            if reader is not None:
                reader.close()
            connection.close()
        if listener is not None:
            listener.close()
    if failure:
        result.update(status="failed", code=failure[0], stage=failure[1], error=failure[2])
    else:
        result.update(status="ok", code="FD" if source else "FP")
    Path(f"/tmp/a3-fabric-{role}.json").write_text(json.dumps(result, indent=2) + "\n")
    if args.run_dir:
        (args.run_dir / "status.json").write_text(json.dumps(result, indent=2) + "\n")
    print_result(result["code"])
    return 1 if failure else 0


def check_one_page(handle, bm, torch, source_gva, result):
    device_k = torch.zeros((LAYERS, 2, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
    device_rope = torch.zeros((LAYERS, 2, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
    torch.npu.synchronize()
    sources, targets, sizes = transfer_plan(source_gva, device_k.data_ptr(), device_rope.data_ptr())
    result["transfer_fragments"] = len(sizes)
    check_rc(handle.copy_data_batch(sources, targets, sizes, len(sizes), bm.BmCopyType.GH2L, 0), "GH2L batch")
    check_rc(handle.wait(), "client BM wait")
    torch.npu.synchronize()
    check_page(device_k, device_rope, 1, 0, torch)
    if torch.count_nonzero(device_k[:, 0]).item() or torch.count_nonzero(device_rope[:, 0]).item():
        raise RuntimeError("reserved L1 page was overwritten")


def supervise(args):
    result_path = Path(f"/tmp/a3-fabric-{args.role}.json")
    result_path.write_text(json.dumps({"status": "running", "code": "RUN"}) + "\n")
    extra = ["--run-dir", str(args.run_dir)] if getattr(args, "run_dir", None) else []
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker", *extra],
                               start_new_session=True, stdout=subprocess.PIPE if extra else None,
                               stderr=subprocess.STDOUT if extra else None, text=True)
    def relay():
        with (args.run_dir / "cli.log").open("w") as log:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
    thread = threading.Thread(target=relay, daemon=True) if extra else None
    if thread:
        thread.start()
    try:
        rc = process.wait(timeout=args.timeout)
        if rc < 0:
            result_path.write_text(json.dumps({"status": "failed", "code": "F9",
                                              "error": f"worker killed by signal {-rc}"}) + "\n")
            print("F9", flush=True)
        return rc if rc >= 0 else 1
    except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        code = "STOP" if isinstance(exc, KeyboardInterrupt) else "FT"
        Path(f"/tmp/a3-fabric-{args.role}.json").write_text(json.dumps({
            "status": "stopped", "code": code, "error": "operator stopped worker" if code == "STOP" else "worker timeout",
        }) + "\n")
        print(code, flush=True)
        return 130 if code == "STOP" else 1
    finally:
        if thread:
            thread.join()
            status = json.loads(result_path.read_text())
            (args.run_dir / "status.json").write_text(json.dumps(status, indent=2) + "\n")
            if status.get("code") in ("STOP", "FT", "F9"):
                with (args.run_dir / "cli.log").open("a") as log:
                    log.write(status["code"] + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("source", "client"))
    parser.add_argument("local_ip", nargs="?")
    parser.add_argument("source_ip", nargs="?")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--performance", action="store_true")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.timeout = args.timeout if args.timeout is not None else (3600 if args.performance else 180)
    if args.diagnose:
        path = Path(f"/tmp/a3-fabric-{args.role}.json")
        if not path.exists():
            print("NO_RESULT")
            return 1
        result = json.loads(path.read_text())
        print(f"{result.get('code')} {result.get('stage', '')}: {result.get('error', '')}".replace("\n", " ")[:240])
        return 0
    if not args.local_ip or (args.role == "client" and not args.source_ip):
        parser.error("source needs its IP; client needs its own IP and the source IP")
    if args.role == "client" and args.local_ip == args.source_ip:
        parser.error("client and source must use distinct node IPs")
    if args.device < 0 or args.timeout <= 0 or args.warmup < 0 or args.repeats < 1:
        parser.error("device must be nonnegative and timeout positive")
    if args.worker:
        return worker(args)
    if args.performance:
        args.run_dir = Path(__file__).resolve().parent / "results" / datetime.now().strftime("%y%m%d_%H%M%S")
        args.run_dir.mkdir(parents=True, exist_ok=False)
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
