"""Ticket03 S4: real paired NPU Index K/aux transport through the original worker.

No model server, BM allocation or TP collective is started. The existing
control owner receives explicit BM-write-complete fixtures; S2 verifies BM
data, and S5/S6 verify the complete model/scheduler path. Native copies here
use AscendTransferEngine, NPUMLATokenToKVPool and MetadataBuffers directly.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import traceback
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from verify_graph import open_channel  # noqa: E402

from ascend_mempool.control import TestChannel
from ascend_mempool.pd_transfer import (  # noqa: E402
    drain_worker,
    make_args,
    make_gate_descriptor,
    make_manager,
    make_sender,
    target_fields,
)

CASES = (
    "bm_first",
    "transfer_first",
    "empty_last",
    "bad_layout",
    "aux_failure",
    "cancel_inflight",
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank", type=int, choices=(0, 1), required=True)
    parser.add_argument("--head-ip", required=True)
    parser.add_argument("--local-ip", required=True)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--store-port", type=int, default=18875)
    parser.add_argument("--control-port", type=int, default=18876)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def pack(message: Any) -> list[str]:
    from sglang.srt.disaggregation.ascend.mempool_protocol import encode_message

    return [frame.hex() for frame in encode_message(message)]


def unpack(frames: list[str]) -> Any:
    from sglang.srt.disaggregation.ascend.mempool_protocol import decode_message

    return decode_message([bytes.fromhex(frame) for frame in frames])


class AuditedEngine:
    """Observe native engine boundaries and inject explicit failure scenarios."""

    def __init__(self, engine: Any, args: Any, pool: Any) -> None:
        self.engine = engine
        self.allowed = list(
            zip(
                args.kv_data_ptrs + args.aux_data_ptrs,
                args.kv_data_lens + args.aux_data_lens,
            )
        )
        self.main = [
            (buf.data_ptr(), buf.nbytes)
            for buf in (pool.k_buffer, pool.v_buffer)
            if buf is not None
        ]
        self.aux = list(zip(args.aux_data_ptrs, args.aux_data_lens))
        self.registered: list[tuple[int, int]] = []
        self.bytes = 0
        self.fail_aux = False
        self.before_copy: Any = None

    def get_session_id(self) -> str:
        return str(self.engine.get_session_id())

    def check(self, ptr: int, length: int) -> None:
        assert length > 0
        assert not any(
            ptr < base + size and base < ptr + length for base, size in self.main
        ), "main KV published/copied"
        assert any(
            base <= ptr and ptr + length <= base + size for base, size in self.allowed
        ), "copy outside published storage"

    def batch_register(self, ptrs: list[int], lengths: list[int]) -> None:
        assert len(ptrs) == len(lengths)
        for ptr, length in zip(ptrs, lengths):
            self.check(ptr, length)
        # Production Ascend batch_register logs a nonzero SDK result at DEBUG.
        # Make that boundary fatal in the gate so registration cannot false-pass.
        ret = self.engine.engine.batch_register_memory(ptrs, lengths)
        if ret != 0:
            raise RuntimeError(f"native registration failed: {ret}")
        self.registered.extend(zip(ptrs, lengths))

    def batch_transfer_sync(
        self, session: str, srcs: list[int], dsts: list[int], lengths: list[int]
    ) -> int:
        assert len(srcs) == len(dsts) == len(lengths)
        for ptr, size in zip(srcs, lengths):
            self.check(ptr, size)
        if self.before_copy is not None:
            callback, self.before_copy = self.before_copy, None
            callback()
        if self.fail_aux and any(
            base <= ptr < base + size for ptr in srcs for base, size in self.aux
        ):
            return -1
        result = int(self.engine.batch_transfer_sync(session, srcs, dsts, lengths))
        if result == 0:
            self.bytes += sum(lengths)
        return result


def verify(args: argparse.Namespace, channel: TestChannel) -> dict[str, Any]:
    import numpy as np
    import torch

    importlib.import_module("torch_npu")
    from sglang.srt.disaggregation.ascend.conn import AscendKVManager, AscendKVSender
    from sglang.srt.disaggregation.ascend.mempool_control import MempoolPDControl
    from sglang.srt.disaggregation.ascend.mempool_protocol import (
        PoolDescriptor,
        PoolPeer,
    )
    from sglang.srt.disaggregation.ascend.transfer_engine import AscendTransferEngine
    from sglang.srt.disaggregation.base.conn import KVArgs, KVPoll
    from sglang.srt.disaggregation.mooncake.conn import KVArgsRegisterInfo
    from sglang.srt.disaggregation.utils import (
        DisaggregationMode,
        MetadataBuffers,
        _apply_metadata_gate,
    )
    from sglang.srt.hardware_backend.npu.memory_pool_npu import NPUMLATokenToKVPool
    from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
        SparseKVOffloadMode as Mode,
    )
    from sglang.srt.observability.trace import TraceNullContext

    descriptor = make_gate_descriptor(PoolDescriptor)
    torch.npu.set_device(args.device_id)
    device = f"npu:{args.device_id}"
    mode = Mode.PD_PREFILL_MEMPOOL if args.rank == 0 else Mode.PD_DECODE_MEMPOOL
    role = DisaggregationMode.PREFILL if args.rank == 0 else DisaggregationMode.DECODE
    pool = NPUMLATokenToKVPool(
        size=5 * 128,
        page_size=128,
        dtype=torch.bfloat16,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
        layer_num=descriptor.layers,
        device=device,
        enable_memory_saver=False,
        index_head_dim=128,
        indexer_layer_ids=(1, 4, 7),
        sparse_kv_offload_mode=mode,
    )
    metadata = MetadataBuffers(6, 16, torch.bfloat16, max_sampling_mask_tokens=16)
    kv_args = make_args(KVArgs, pool, metadata.output_ids)
    kv_args.aux_data_ptrs, kv_args.aux_data_lens, kv_args.aux_item_lens = (
        metadata.get_buf_infos()
    )
    assert type(kv_args) is KVArgs
    native_engine = AscendTransferEngine(args.local_ip, args.device_id, role)
    engine = AuditedEngine(native_engine, kv_args, pool)
    manager = make_manager(AscendKVManager, kv_args, engine, role)
    # Same order as server startup: filtered addresses register first, then
    # service finalizes logical fields before receiver metadata publication.
    manager.register_buffer_to_engine()
    layout = manager.configure_mempool_transfer(pool)
    assert layout.layer_ids == (1, 4, 7) and kv_args.kv_buf_groups == 1
    assert len(engine.registered) == 3 + len(kv_args.aux_data_ptrs)
    control = MempoolPDControl(
        PoolPeer(
            f"gate-{args.rank}-{os.getpid()}",
            role.value,
            0,
            16,
            1,
            0,
            descriptor,
            "index_k_only",
            engine.get_session_id(),
            layout,
        )
    )
    manager.mempool_control = control
    if args.rank == 1:
        channel.send("HELLO", **{"frames": pack(control.begin_handshake("tcp://d:1"))})
        control.apply(unpack(channel.expect("READY")["frames"]))
        channel.send("REGISTRATION", **target_fields(kv_args, engine.get_session_id()))
    else:
        ready = control.apply(unpack(channel.expect("HELLO")["frames"]))
        channel.send("READY", **{"frames": pack(ready)})
        target = KVArgsRegisterInfo(**channel.expect("REGISTRATION"))
        manager.decode_kv_args_table[target.mooncake_session_id] = target
    reports = []
    tensors = {
        name: value
        for name, value in vars(metadata).items()
        if isinstance(value, torch.Tensor)
    }
    for case_index, case in enumerate(CASES):
        room = 100 + case_index
        empty = case == "empty_last"
        failed = case in ("bad_layout", "aux_failure", "cancel_inflight")
        pages = np.array([] if empty else [5, 2], np.int32)
        pool.index_k_buffer.fill_(-9)
        for tensor in tensors.values():
            tensor.zero_()
        if args.rank == 0:
            for i, layer in enumerate((1, 4, 7)):
                pool.index_k_buffer[i, 1].fill_(10 * layer + 1)
                pool.index_k_buffer[i, 4].fill_(10 * layer + 4)
            for i, name in enumerate(sorted(tensors)):
                tensors[name][1].fill_(i + 1)
            metadata.output_ids[1, 0] = 123 + case_index
            metadata.bootstrap_room[1, 0] = room
        torch.npu.synchronize()
        if args.rank == 1:
            acquire = control.acquire_decode(room, case, 3, 256, 32, "tcp://d:1")
            request = acquire.request
            assert request is not None
            channel.send("ACQUIRE", **{"frames": pack(acquire)})
            bound = control.apply(unpack(channel.expect("ACQUIRED")["frames"]))
            channel.send("BOUND", **{"frames": pack(bound)})
            before = channel.expect("BEFORE_TRANSFER")
            if before["ready"]:
                control.apply(unpack(before["ready"]))
            assert not control.can_decode(request)
            channel.send("COPY", **{})
            result = channel.expect("TRANSFER_RESULT")
            assert result["status"] == (KVPoll.Failed if failed else KVPoll.Success)
            assert result["outstanding"] == 0
            assert result["abort_acks"] == ([room] if case == "cancel_inflight" else [])
            torch.npu.synchronize()
            expected = torch.full_like(pool.index_k_buffer, -9, device="cpu")
            if not empty and case != "bad_layout":
                for i, layer in enumerate((1, 4, 7)):
                    expected[i, 5].fill_(10 * layer + 1)
                    expected[i, 2].fill_(10 * layer + 4)
            torch.testing.assert_close(
                pool.index_k_buffer.cpu(), expected, rtol=0, atol=0
            )
            aux_sent = case not in ("bad_layout", "aux_failure")
            for i, name in enumerate(sorted(tensors)):
                expected_aux = torch.zeros_like(tensors[name], device="cpu")
                if aux_sent:
                    expected_aux[3].fill_(i + 1)
                    if name == "output_ids":
                        expected_aux[3, 0] = 123 + case_index
                    elif name == "bootstrap_room":
                        expected_aux[3, 0] = room
                torch.testing.assert_close(
                    tensors[name].cpu(), expected_aux, rtol=0, atol=0
                )
            expected_bytes = (
                0 if empty or case == "bad_layout" else 2 * sum(kv_args.kv_item_lens)
            )
            if aux_sent:
                expected_bytes += sum(kv_args.aux_item_lens)
            assert result["bytes"] == expected_bytes
            if failed:
                cancel = control.cancel_local(request, case)
                channel.send("CANCEL", **{"frames": pack(cancel)})
            else:
                # Inject delayed metadata visibility after actual aux delivery.
                # The original gate must hold Success until room metadata lands.
                received = metadata.bootstrap_room[3].clone()
                metadata.bootstrap_room[3].zero_()
                req = SimpleNamespace(
                    req=SimpleNamespace(
                        bootstrap_host=args.head_ip, bootstrap_room=room
                    ),
                    metadata_buffer_index=3,
                )
                polls = [KVPoll.Success]
                _apply_metadata_gate(polls, [req], metadata)
                assert polls == [KVPoll.Transferring] and not control.can_decode(
                    request
                )
                metadata.bootstrap_room[3].copy_(received)
                polls = [KVPoll.Success]
                _apply_metadata_gate(polls, [req], metadata)
                assert polls == [KVPoll.Success]
                control.transfer_succeeded(request)
                if result["ready"]:
                    assert not control.can_decode(request)
                    control.apply(unpack(result["ready"]))
                assert control.can_decode(request)
                control.start_decode(request)
            control.begin_drain(request)
            torch.npu.synchronize()
            channel.send(
                "DONE", **{"frames": pack(control.finish_drain(request, case))}
            )
            control.apply(unpack(channel.expect("ACK")["frames"]))
            assert (
                control.state(request) == "CLOSED"
                and len(control.available_slots()) == 16
            )
            channel.send("VERIFIED", **{"case": case, "bytes": result["bytes"]})
        else:
            acquire = unpack(channel.expect("ACQUIRE")["frames"])
            request = acquire.request
            assert request is not None
            control.apply(acquire)
            channel.send(
                "ACQUIRED", **{"frames": pack(control.acquire_prefill(request, 7))}
            )
            control.apply(unpack(channel.expect("BOUND")["frames"]))
            control.start_prefill(request)
            # Control fixture: S4 tests native transport independently of BM.
            control.finish_prefill_writes(request)
            ready_frames = pack(control.publish_kv_ready(request, 256))
            bm_first = case != "transfer_first"
            channel.send(
                "BEFORE_TRANSFER", **{"ready": ready_frames if bm_first else None}
            )
            channel.expect("COPY")
            statuses: list[dict[str, Any]] = []
            acks: list[int] = []
            manager.send_kv_status_message = lambda **event: statuses.append(event)
            manager._send_abort_ack = lambda ip, port, r: acks.append(r)
            engine.bytes = 0
            engine.fail_aux = case == "aux_failure"
            manager.failed_sessions.clear()
            sender = make_sender(AscendKVSender, manager, room, pages, KVPoll)
            sender.trace_ctx = TraceNullContext()
            if case == "bad_layout":
                target.dst_kv_layer_ids = [0, 1, 2]
            if case == "cancel_inflight":

                def cancel_during_copy() -> None:
                    manager.update_status(room, KVPoll.Failed)
                    manager.register_deferred_ack_target(room, "d", 1)
                    manager._maybe_ack_drained_abort(room)
                    assert not acks, "abort acknowledged before copy returned"

                engine.before_copy = cancel_during_copy
            if case in ("bm_first", "transfer_first"):
                sender.send(np.array([1], np.int32))
                drain_worker(manager)
                assert not statuses and room in manager.transfer_infos
                sender.send(np.array([4], np.int32))
            else:
                sender.send(np.array([] if empty else [1, 4], np.int32))
            drain_worker(manager)
            assert statuses and room not in manager.transfer_infos
            assert manager._staging_outstanding.get(room, 0) == 0
            # Handoff has drained: native P storage can now be reused while its
            # independent BM control lease remains owned until DONE.
            pool.k_buffer.fill_(99)
            pool.v_buffer.fill_(98)
            torch.npu.synchronize()
            assert 7 not in control.available_slots()
            channel.send(
                "TRANSFER_RESULT",
                **{
                    "status": statuses[-1]["status"],
                    "bytes": engine.bytes,
                    "outstanding": 0,
                    "abort_acks": acks,
                    "ready": None if bm_first else ready_frames,
                },
            )
            if failed:
                control.apply(unpack(channel.expect("CANCEL")["frames"]))
            ack = control.apply(unpack(channel.expect("DONE")["frames"]))
            assert ack is not None and len(control.available_slots()) == 16
            channel.send("ACK", **{"frames": pack(ack)})
            channel.expect("VERIFIED")
            target.dst_kv_layer_ids = list(layout.layer_ids)
            result = {"bytes": engine.bytes}
        report = {"case": case, "bytes": result["bytes"], "passed": True}
        reports.append(report)
        print("PD_TRANSFER_PASS", json.dumps(report), flush=True)
    # Both peers have verified every destination before any storage is freed.
    channel.send("FINISHED", **{})
    channel.expect("FINISHED")
    torch.npu.synchronize()
    return {
        "cases": reports,
        "main_kv_registered_entries": 0,
        "main_kv_sent_bytes": 0,
        "index_k_layer_ids": list(layout.layer_ids),
        "aux_entries": len(kv_args.aux_data_ptrs),
        "scope": "one paired TP rank; real native transfer; BM readiness fixture; no model/TP collective",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    # Only the independent component gate opts into formal pools. It does not
    # bypass the model runner's S5 launch guard or request a model server.
    os.environ.update(
        SGLANG_NPU_ENABLE_MEMPOOL="0",
        SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD="0",
        SGLANG_USE_FIA_NZ="0",
        ASCEND_MF_TRANSFER_PROTOCOL="sdma",
        ASCEND_MF_STORE_URL=f"tcp://{args.head_ip}:{args.store_port}",
    )
    channel = None
    report: dict[str, Any] = {"rank": args.rank, "command": sys.argv, "success": False}
    try:
        report["git_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        report["versions"] = {}
        for name in ("torch", "torch-npu", "memfabric-hybrid", "sgl-kernel-npu"):
            try:
                report["versions"][name] = version(name)
            except PackageNotFoundError:
                report["versions"][name] = "not installed"
        channel = open_channel(args)
        environment = {"git_sha": report["git_sha"], "versions": report["versions"]}
        channel.send("ENVIRONMENT", **environment)
        if channel.expect("ENVIRONMENT") != environment:
            raise RuntimeError("P/D must use the same commit and package versions")
        report.update(verify(args, channel))
        report["success"] = True
    except Exception as exc:
        traceback.print_exc()
        report["error"] = f"{type(exc).__name__}: {exc}"
        if channel is not None:
            try:
                channel.send("ERROR", **{"error": report["error"]})
            except OSError:
                pass
    finally:
        if channel is not None:
            channel.close()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        "ALL_CHECKS_PASSED" if report["success"] else "PD_TRANSFER_FAILED", flush=True
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
