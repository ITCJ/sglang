"""PD publication and transfer checks with CPU memory at the device boundary."""

import ast
import ctypes
import dataclasses
import enum
import logging
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from msgspec.structs import replace
from test_native_release import load_methods

from ascend_mempool.pd_transfer import (
    drain_worker,
)
from ascend_mempool.pd_transfer import make_args as fill_args
from ascend_mempool.pd_transfer import (
    make_gate_descriptor,
    make_manager,
    make_sender,
    target_fields,
)
from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_protocol import (
    IndexKTransferLayout,
    PoolDescriptor,
    PoolPeer,
)
from ascend_sparse.config import SparseKVOffloadMode as Mode


def load_functions(path, names, namespace):
    source = Path(__file__).resolve().parents[3] / "python/sglang/srt" / path
    tree = ast.parse(source.read_text())
    tree.body = [
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
    ] + [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    exec(compile(ast.fix_missing_locations(tree), str(source), "exec"), namespace)


def transport_classes():
    namespace = dict(
        np=np,
        deque=deque,
        time=time,
        enum=enum,
        Enum=enum.Enum,
        dataclasses=dataclasses,
        logger=logging.getLogger(__name__),
        IndexKTransferLayout=IndexKTransferLayout,
        FAKE_BOOTSTRAP_HOST="fake",
        get_memory=lambda: SimpleNamespace(enable_unified_memory=False),
        mooncake_trace_func=lambda stage: lambda method: method,
        MooncakeRequestStage=SimpleNamespace(MOONCAKE_SEND=None),
        NetworkAddress=lambda ip, port: SimpleNamespace(
            to_host_port_str=lambda: f"{ip}:{port}"
        ),
        envs=SimpleNamespace(
            SGLANG_MOONCAKE_SEND_AUX_TCP=SimpleNamespace(get=lambda: False)
        ),
    )
    load_functions(
        "disaggregation/base/conn.py", {"KVArgs", "KVPoll", "StateType"}, namespace
    )
    load_functions(
        "disaggregation/utils.py",
        {
            "build_transfer_entry_pairs",
            "DisaggregationMode",
            "_apply_metadata_gate",
            "_is_fake_transfer",
        },
        namespace,
    )
    namespace["TraceNullContext"] = SimpleNamespace
    load_functions(
        "disaggregation/common/utils.py",
        {"group_concurrent_contiguous", "TransferKVChunk"},
        namespace,
    )
    common = load_methods(
        "disaggregation/common/conn.py",
        "CommonKVManager",
        {
            "check_status",
            "update_status",
            "record_failure",
            "conclude_transfer",
            "conclude_failure",
            "_room_notify_targets",
            "_prefill_unique_rank",
            "_maybe_ack_drained_abort",
            "register_deferred_ack_target",
            "_should_skip_cp_replicated_state_transfer",
        },
        namespace,
        standalone=True,
    )
    namespace["CommonKVManager"] = common
    namespace["StagingManagerMixin"] = type("StagingManagerMixin", (), {})
    base = load_methods(
        "disaggregation/mooncake/conn.py",
        "MooncakeKVManager",
        {
            "_send_kvcache_generic",
            "send_aux",
            "_transfer_data",
            "get_session_id",
            "transfer_worker",
            "_get_dsa_cache_transfer_skip_flags",
            "add_transfer_request",
            "maybe_send_extra",
            "_validate_envelope_kv_layout",
        },
        namespace,
    )
    namespace["MooncakeKVManager"] = base
    manager = load_methods(
        "disaggregation/ascend/conn.py",
        "AscendKVManager",
        {
            "configure_mempool_transfer",
            "_validate_mempool_target",
            "send_kvcache",
            "update_status",
            "send_aux",
            "maybe_send_extra",
            "_send_mempool_index_k",
            "register_buffer_to_engine",
            "_validate_envelope_kv_layout",
            "_get_sparse_pd_main_layer_count",
            "_get_sparse_pd_source_layer_ids",
        },
        namespace,
    )
    sender_base = load_methods(
        "disaggregation/common/conn.py",
        "CommonKVSender",
        {"_prepare_send_indices", "_record_transfer_indices"},
        namespace,
        standalone=True,
    )
    namespace["CommonKVSender"] = sender_base
    namespace["MooncakeFailureExceptionMixin"] = type(
        "MooncakeFailureExceptionMixin", (), {}
    )
    sender = load_methods(
        "disaggregation/mooncake/conn.py", "MooncakeKVSender", {"send"}, namespace
    )
    return manager, sender, SimpleNamespace(**namespace)


MANAGER, SENDER, API = transport_classes()


def make_args(pool):
    aux = torch.zeros((6, 2), dtype=torch.int32)
    return fill_args(API.KVArgs, pool, aux), aux


class CopyEngine:
    def __init__(self):
        self.registered = []
        self.copies = []
        self.fail_aux = False
        self.before_copy = lambda: None

    def get_session_id(self):
        return "p:1"

    def batch_register(self, ptrs, lengths):
        self.registered += list(zip(ptrs, lengths))

    def batch_transfer_sync(self, session, srcs, dsts, lengths):
        self.before_copy()
        if self.fail_aux and lengths == [8]:
            return -1
        for src, dst, size in zip(srcs, dsts, lengths):
            self.copies.append((src, dst, size))
            ctypes.memmove(dst, src, size)
        return 0


def make_pool(mode):
    cls = load_methods(
        "hardware_backend/npu/memory_pool_npu.py",
        "NPUMLATokenToKVPool",
        {
            "get_contiguous_buf_infos",
            "get_state_buf_infos",
            "get_kv_layer_ids",
            "get_state_layer_ids",
            "_raise_if_native_kv_cache_disabled",
        },
        dict(torch=torch, SparseKVOffloadMode=Mode),
        standalone=True,
    )
    pool = cls()
    pool.sparse_kv_offload_mode = mode
    pool.layer_num, pool.start_layer, pool.page_size = 8, 0, 4
    pool.dtype = pool.store_dtype = torch.bfloat16
    pool.index_head_dim = 2
    pool.indexer_layer_ids = (1, 4, 7)
    pool.index_k_buffer = torch.zeros((3, 6, 4, 1, 2), dtype=torch.bfloat16)
    pool.index_k_scale_buffer = None
    pool.dsa_kv_cache_store_fp8 = False
    pool.k_buffer = pool.v_buffer = None
    if not mode.uses_sparse_kv_cache:
        pool.k_buffer = torch.full((8, 6, 4, 1, 4), 11, dtype=torch.bfloat16)
        pool.v_buffer = torch.full((8, 6, 4, 1, 2), 22, dtype=torch.bfloat16)
    return pool


class TestPDPublication(unittest.TestCase):
    def test_npu_gate_control_layout_is_valid(self):
        # Run the gate's actual fixture through production bounds validation
        # on CPU, before the paired NPU test can reach its first handshake.
        descriptor = make_gate_descriptor(PoolDescriptor)
        self.assertEqual(descriptor.layers, 8)
        self.assertEqual(descriptor.prompt_bytes, 37748736)
        self.assertEqual(descriptor.decode_bytes, 4718592)
        self.assertGreaterEqual(descriptor.stride_bytes, descriptor.prompt_bytes)

    def test_formal_both_sides_publish_only_actual_indexer_layers(self):
        for mode in (Mode.PD_PREFILL_MEMPOOL, Mode.PD_DECODE_MEMPOOL):
            with self.subTest(mode=mode):
                pool = make_pool(mode)
                ptrs, lengths, strides = pool.get_contiguous_buf_infos()
                self.assertEqual(ptrs, [b.data_ptr() for b in pool.index_k_buffer])
                self.assertEqual(lengths, [96, 96, 96])
                self.assertEqual(strides, [16, 16, 16])
                self.assertEqual(pool.get_kv_layer_ids(), [1, 4, 7])
                if mode is Mode.PD_PREFILL_MEMPOOL:
                    self.assertTrue((pool.k_buffer == 11).all())
                    self.assertTrue((pool.v_buffer == 22).all())
                else:
                    self.assertIsNone(pool.k_buffer)
                    self.assertIsNone(pool.v_buffer)


class TestPDTransfer(unittest.TestCase):
    def setUp(self):
        self.p = make_pool(Mode.PD_PREFILL_MEMPOOL)
        self.d = make_pool(Mode.PD_DECODE_MEMPOOL)
        self.args, self.p_aux = make_args(self.p)
        self.dst_args, self.d_aux = make_args(self.d)
        self.manager = make_manager(
            MANAGER, self.args, CopyEngine(), API.DisaggregationMode.PREFILL
        )
        self.statuses = []
        self.acks = []
        self.manager.send_kv_status_message = lambda **kwargs: self.statuses.append(
            kwargs
        )
        self.manager._send_abort_ack = lambda ip, port, room: self.acks.append(room)

    def test_local_contract_rejects_wrong_page_size_and_quantized_index(self):
        self.args.page_size *= 2
        with self.assertRaisesRegex(ValueError, "page size"):
            self.manager.configure_mempool_transfer(self.p)
        self.args.page_size = self.p.page_size
        self.p.index_k_scale_buffer = torch.ones(1)
        with self.assertRaisesRegex(ValueError, "BF16 Index K"):
            self.manager.configure_mempool_transfer(self.p)

    def connect(self):
        layout = self.manager.configure_mempool_transfer(self.p)
        bm = PoolDescriptor(8, 16, 8, 16, 1, 576, "bfloat16", 1179648, 2359296, 2359296)
        p = PoolPeer("p", "prefill", 0, 16, 1, 0, bm, "index_k_only", "p:1", layout)
        d = PoolPeer("d", "decode", 0, 16, 1, 0, bm, "index_k_only", "d:2", layout)
        self.p_control, self.d_control = MempoolPDControl(p), MempoolPDControl(d)
        self.d_control.apply(
            self.p_control.apply(self.d_control.begin_handshake("tcp://d:2"))
        )
        self.manager.mempool_control = self.p_control
        self.dst_args.kv_layer_ids = [1, 4, 7]
        self.target = SimpleNamespace(**target_fields(self.dst_args, "d:2"))
        self.manager.decode_kv_args_table = {"d:2": self.target}

    def test_noncontiguous_pages_copy_index_k_and_aux_without_touching_main_kv(self):
        self.manager.register_buffer_to_engine()
        self.connect()
        self.assertEqual(self.args.kv_layer_ids, [1, 4, 7])
        self.assertEqual(self.args.kv_buf_groups, 1)
        self.assertEqual(len(self.manager.engine.registered), 4)
        self.d.index_k_buffer.fill_(-9)
        self.p.index_k_buffer[:, 1].fill_(12)
        self.p.index_k_buffer[:, 4].fill_(34)
        self.p_aux[1] = torch.tensor([123, 456])
        result = self.manager.send_kvcache(
            "d:2",
            np.array([1, 4], np.int32),
            self.target.dst_kv_ptrs,
            np.array([5, 2], np.int32),
            None,
            dst_layer_ids=[1, 4, 7],
        )
        self.assertEqual(result, 0)
        expected = torch.full_like(self.d.index_k_buffer, -9)
        expected[:, 5], expected[:, 2] = 12, 34
        torch.testing.assert_close(self.d.index_k_buffer, expected, rtol=0, atol=0)
        req = SimpleNamespace(mooncake_session_id="d:2", dst_aux_index=3)
        self.assertEqual(self.manager.send_aux(req, 1, self.target.dst_aux_ptrs), 0)
        self.assertEqual(self.d_aux[3].tolist(), [123, 456])
        self.assertEqual(sum(n for _, _, n in self.manager.engine.copies), 104)
        self.assertTrue((self.p.k_buffer == 11).all())


class TestPDWorker(TestPDTransfer):
    def sender(self, room=101, empty=False):
        return make_sender(
            SENDER,
            self.manager,
            room,
            np.array([] if empty else [5, 2], np.int32),
            API.KVPoll,
        )

    def test_multi_chunk_and_empty_final_keep_completion_and_aux(self):
        self.connect()
        self.p.index_k_buffer[:, 1].fill_(12)
        self.p.index_k_buffer[:, 4].fill_(34)
        self.p_aux[1] = torch.tensor([123, 456])
        sender = self.sender()
        sender.send(np.array([1], np.int32))
        drain_worker(self.manager)
        self.assertEqual(self.statuses, [])
        self.assertEqual(self.d_aux[3].tolist(), [0, 0])
        self.assertEqual(self.manager._staging_outstanding[101], 0)
        sender.send(np.array([4], np.int32))
        drain_worker(self.manager)
        self.assertEqual(self.statuses[-1]["status"], API.KVPoll.Success)
        self.assertNotIn(101, self.manager.transfer_infos)
        self.assertNotIn(101, self.manager._staging_outstanding)
        self.assertEqual(self.d_aux[3].tolist(), [123, 456])
        self.assertTrue((self.d.index_k_buffer[:, 5] == 12).all())
        self.assertTrue((self.d.index_k_buffer[:, 2] == 34).all())
        sender = self.sender(room=102, empty=True)
        self.p_aux[1, 0] = 789
        sender.send(np.array([], np.int32))
        drain_worker(self.manager)
        self.assertEqual(self.statuses[-1]["status"], API.KVPoll.Success)
        self.assertEqual(self.d_aux[3, 0].item(), 789)
        self.assertNotIn(102, self.manager._staging_outstanding)

    def test_bad_destination_and_empty_chunk_cannot_report_success(self):
        for empty in (False, True):
            for field, bad in (
                ("dst_kv_layer_ids", [0, 1, 2]),
                ("dst_kv_item_lens", [16, 32, 16]),
                ("dst_aux_ptrs", []),
            ):
                with self.subTest(empty=empty, field=field):
                    self.setUp()
                    self.connect()
                    setattr(self.target, field, bad)
                    self.sender(empty=empty).send(
                        np.array([] if empty else [1, 4], np.int32)
                    )
                    drain_worker(self.manager)
                    self.assertEqual(self.statuses[-1]["status"], API.KVPoll.Failed)
                    self.assertEqual(self.manager.engine.copies, [])
                    self.assertNotIn(101, self.manager._staging_outstanding)
                    self.assertNotIn(101, self.manager.transfer_infos)

    def test_aux_failure_keeps_failed_status_and_drains(self):
        self.connect()
        self.manager.engine.fail_aux = True
        self.sender().send(np.array([1, 4], np.int32))
        drain_worker(self.manager)
        self.assertEqual(self.manager.check_status(101), API.KVPoll.Failed)
        self.manager.conclude_transfer(bootstrap_room=101, status=API.KVPoll.Success)
        self.assertTrue(all(s["status"] == API.KVPoll.Failed for s in self.statuses))
        self.assertNotIn(101, self.manager._staging_outstanding)
        self.assertEqual(self.d_aux[3].tolist(), [0, 0])

    def test_cancel_during_copy_waits_for_worker_drain(self):
        self.connect()

        def cancel():
            self.manager.update_status(101, API.KVPoll.Failed)
            self.manager.register_deferred_ack_target(101, "d", 2)
            self.manager._maybe_ack_drained_abort(101)
            self.assertEqual(self.acks, [])
            self.manager.engine.before_copy = lambda: None

        self.manager.engine.before_copy = cancel
        self.sender().send(np.array([1, 4], np.int32))
        drain_worker(self.manager)
        self.assertEqual(self.acks, [101])
        self.assertEqual(self.statuses[-1]["status"], API.KVPoll.Failed)
        self.assertNotIn(101, self.manager._staging_outstanding)

    def test_ordinary_prefill_still_copies_native_kv_and_index_k(self):
        for mode in (Mode.DISABLED, Mode.PD_PREFILL_NATIVE):
            with self.subTest(mode=mode):
                p, d = make_pool(mode), make_pool(mode)
                args, aux = make_args(p)
                dst, dst_aux = make_args(d)
                manager = make_manager(
                    MANAGER, args, CopyEngine(), API.DisaggregationMode.PREFILL
                )
                self.assertIsNone(manager.configure_mempool_transfer(p))
                self.assertEqual(len(args.kv_data_ptrs), 19)
                self.assertEqual(args.kv_layer_ids, [])
                manager.register_buffer_to_engine()
                d.k_buffer.fill_(-9)
                d.v_buffer.fill_(-9)
                p.index_k_buffer[:, 1].fill_(12)
                ret = manager.send_kvcache(
                    "d:2",
                    np.array([1], np.int32),
                    dst.kv_data_ptrs,
                    np.array([5], np.int32),
                    None,
                )
                self.assertEqual(ret, 0)
                self.assertTrue((d.k_buffer[:, 5] == 11).all())
                self.assertTrue((d.v_buffer[:, 5] == 22).all())
                self.assertTrue((d.index_k_buffer[:, 5] == 12).all())
                self.assertTrue((d.k_buffer[:, 1] == -9).all())

    def test_session_mismatch_blocks_all_copies(self):
        self.connect()
        self.p_control.peer = replace(self.p_control.peer, transport_session="stale:2")
        self.sender().send(np.array([1, 4], np.int32))
        # Keep the native registration from the original sender; only its peer
        # contract changed, as with a restarted native session.
        self.manager.transfer_infos[101] = {
            "d:2": SimpleNamespace(
                room=101,
                mooncake_session_id="d:2",
                is_dummy=False,
                dst_kv_indices=np.array([5, 2], np.int32),
                dst_device_kv_indices=None,
                dst_aux_index=3,
                endpoint="d",
                dst_port=2,
                required_dst_info_num=1,
            )
        }
        drain_worker(self.manager)
        self.assertEqual(self.statuses[-1]["status"], API.KVPoll.Failed)
        self.assertEqual(self.manager.engine.copies, [])

    def test_joint_ready_and_metadata_follow_real_transfer_completion(self):
        for bm_first in (True, False):
            with self.subTest(bm_first=bm_first):
                self.setUp()
                self.connect()
                acquire = self.d_control.acquire_decode(
                    101, "real-transfer", 3, 8, 8, "tcp://d:2"
                )
                request = acquire.request
                self.p_control.apply(acquire)
                self.p_control.apply(
                    self.d_control.apply(self.p_control.acquire_prefill(request, 7))
                )
                self.p_control.start_prefill(request)
                self.p_control.finish_prefill_writes(request)
                ready = self.p_control.publish_kv_ready(request, 8)
                if bm_first:
                    self.d_control.apply(ready)
                self.assertFalse(self.d_control.can_decode(request))
                self.sender().send(np.array([1, 4], np.int32))
                drain_worker(self.manager)
                self.assertEqual(self.statuses[-1]["status"], API.KVPoll.Success)
                metadata = SimpleNamespace(
                    bootstrap_room=torch.zeros((1, 1), dtype=torch.int64)
                )
                req = SimpleNamespace(
                    req=SimpleNamespace(bootstrap_room=101, bootstrap_host="p"),
                    metadata_buffer_index=0,
                )
                polls = [API.KVPoll.Success]
                API._apply_metadata_gate(polls, [req], metadata)
                self.assertEqual(polls, [API.KVPoll.Transferring])
                self.assertFalse(self.d_control.can_decode(request))
                metadata.bootstrap_room[0, 0] = 101
                polls = [API.KVPoll.Success]
                API._apply_metadata_gate(polls, [req], metadata)
                self.assertEqual(polls, [API.KVPoll.Success])
                self.d_control.transfer_succeeded(request)
                if not bm_first:
                    self.assertFalse(self.d_control.can_decode(request))
                    self.d_control.apply(ready)
                self.assertTrue(self.d_control.can_decode(request))
                # Native source storage may be reused after handoff; the old
                # BM lease must remain held independently until DONE.
                self.p.k_buffer.fill_(99)
                self.assertNotIn(7, self.p_control.available_slots())
                self.d_control.start_decode(request)
                self.d_control.begin_drain(request)
                self.d_control.apply(
                    self.p_control.apply(self.d_control.finish_drain(request))
                )
                self.assertEqual(self.d_control.state(request), "CLOSED")
                self.assertEqual(len(self.p_control.available_slots()), 16)
                self.assertEqual(len(self.d_control.available_slots()), 16)


if __name__ == "__main__":
    unittest.main()
