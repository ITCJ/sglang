"""Exercise the real manager constructor with CPU device/allocator boundaries.

The full NPU pool and request allocator are exercised by verify_resources.py.
Here the serving stack's pool type and allocator are minimal boundary doubles;
all sparse buffer allocation, hooks and copies run the production manager.
"""

import importlib
import logging
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import torch
from test_materialize import CPUEvent, CPUStream, cpu_copy, cpu_lookup
from test_native_release import load_methods

from ascend_sparse.config import SparseKVOffloadMode as Mode


class TestSparseResources(unittest.TestCase):
    def setUp(self):
        kernel = ModuleType("sgl_kernel_npu.sparsity_driven_kv_offload")
        kernel.create_shm_tensor = None
        kernel.slot_map_lookup = cpu_lookup
        kernel.unidex_copy_inplace = cpu_copy
        constants = ModuleType("sglang.srt.constants")
        constants.GPU_MEMORY_TYPE_KV_CACHE = "kv_cache"
        memory = ModuleType("sglang.srt.mem_cache.memory_pool")
        memory.MLATokenToKVPool = type("MLATokenToKVPool", (), {})
        saver = ModuleType("sglang.srt.utils.torch_memory_saver_adapter")
        saver.TorchMemorySaverAdapter = SimpleNamespace(
            create=lambda **kw: SimpleNamespace(region=lambda *a: nullcontext())
        )
        self.enterContext(
            patch.dict(
                sys.modules, {m.__name__: m for m in (kernel, constants, memory, saver)}
            )
        )
        self.module = importlib.import_module("ascend_sparse.manager")
        self.shm = self.enterContext(
            patch.object(
                self.module, "create_shm_tensor", side_effect=self.allocate_shm
            )
        )
        self.enterContext(patch.object(self.module, "unidex_copy_inplace", cpu_copy))
        self.enterContext(
            patch.object(
                torch,
                "npu",
                SimpleNamespace(
                    Stream=CPUStream,
                    Event=CPUEvent,
                    current_device=lambda: 0,
                    current_stream=CPUStream,
                    synchronize=lambda: None,
                    stream=lambda s: nullcontext(),
                ),
                create=True,
            )
        )
        native = memory.MLATokenToKVPool()
        native.start_layer = 5
        native.layer_num = 2
        native.kv_lora_rank = 2
        native.qk_rope_head_dim = 2
        native.store_dtype = torch.bfloat16
        native.page_size = 4
        self.native = native
        self.rows = SimpleNamespace(
            req_to_token=torch.zeros((3, 12), dtype=torch.int32),
            max_context_len=12,
            device="cpu",
            alloc=Mock(),
            free=Mock(),
            clear=Mock(),
        )
        self.allocator = SimpleNamespace(get_kvcache=lambda: self.native)

    # unittest.TestCase.enterContext is only available on Python 3.11+.
    def enterContext(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    @staticmethod
    def allocate_shm(*, shape, dtype, **kwargs):
        buffer = torch.zeros(shape, dtype=dtype)
        return buffer, buffer.data_ptr(), buffer.data_ptr()

    def make_cache(self, mode):
        return self.module.SparseKVCacheManager(
            self.rows, self.allocator, sparse_context_len=4, mode=mode
        )

    def test_formal_constructor_allocates_cache_but_no_host_or_staging(self):
        self.shm.side_effect = AssertionError("formal D must never allocate host SHM")
        cache = self.make_cache(Mode.PD_DECODE_MEMPOOL)
        self.assertEqual(sum(b.nbytes for b in cache.device_kv_buffer), 192)
        self.assertTrue(all((m == -1).all() for m in cache.device_slot_map))
        self.assertEqual(cache.host_kv_buffer, [])
        self.assertEqual(cache.host_ptr_list, [])
        self.assertEqual(cache.dev_ptr_list, [])
        self.assertIsNone(cache.host_kv_ctx_len)
        self.assertIsNone(cache.pd_decode_k_staging)
        self.assertIsNone(cache.pd_decode_v_staging)
        self.assertIsNone(cache._pd_decode_copy_stream)

    def test_formal_rejects_every_legacy_host_entry_before_access(self):
        from ascend_mempool_pd.sparse_pd import SparsePDDecodeStagingPool

        cache = self.make_cache(Mode.PD_DECODE_MEMPOOL)
        for entry in (cache.offload, cache.offload_v2):
            with (
                self.subTest(entry=entry.__name__),
                self.assertRaisesRegex(RuntimeError, "host KV.*disabled"),
            ):
                entry(None, None, None, None, None)
        with self.assertRaisesRegex(RuntimeError, "host KV.*disabled"):
            cache.get_forward_kv(None, None)
        for entry in (
            lambda: cache.ensure_pd_decode_staging_buffers(),
            lambda: cache.get_pd_decode_transfer_buf_infos(),
            lambda: cache.offload_pd_decode_staging_to_host(0, 1, 2),
            lambda: SparsePDDecodeStagingPool(cache, page_size=4),
        ):
            with self.assertRaisesRegex(RuntimeError, "staging.*disabled"):
                entry()
        self.assertEqual(self.shm.call_count, 0)
        self.assertIsNone(cache.pd_decode_k_staging)

    def test_formal_request_reuse_and_clear_need_no_host_metadata(self):
        def allocate(reqs):
            for req in reqs:
                req.kv.req_pool_idx = 1
            return [1] * len(reqs)

        self.rows.alloc.side_effect = allocate
        original_free = self.rows.free
        original_clear = self.rows.clear
        cache = self.make_cache(Mode.PD_DECODE_MEMPOOL)
        req = SimpleNamespace(
            kv=SimpleNamespace(req_pool_idx=None),
            origin_input_ids=[11, 22],
            bootstrap_room=42,
            inflight_middle_chunks=0,
        )
        for reuse in range(2):
            for table in cache.device_slot_map:
                table[1].fill_(2)  # Old row/slot binding must never become a hit.
            req.kv.req_pool_idx = None
            req.bootstrap_room += reuse
            self.assertEqual(self.rows.alloc([req]), [1])
            cache.init_req(req)
            self.assertTrue(all((m[1] == -1).all() for m in cache.device_slot_map))
            self.assertEqual(cache._pd_room_to_req_pool_idx, {})
            self.assertEqual(cache._pd_room_to_input_len, {})
            self.assertEqual(cache._pd_req_pool_idx_to_room, {})
            for table in cache.device_slot_map:
                table[1, 0] = 0
            # An existing request continuing another chunk keeps its cache.
            self.rows.alloc([req])
            req.inflight_middle_chunks = 1
            cache.init_req(req)
            self.assertTrue(all(m[1, 0] == 0 for m in cache.device_slot_map))
            req.inflight_middle_chunks = 0
            self.rows.free(req)
        self.assertEqual(original_free.call_count, 2)
        self.rows.clear()
        original_clear.assert_called_once_with()
        self.assertTrue(all((m == -1).all() for m in cache.device_slot_map))

    def test_ordinary_modes_restore_host_write_read_and_pd_staging(self):
        import numpy as np

        from ascend_mempool_pd.sparse_pd import (
            SparsePDDecodeStagingPool,
            is_sparse_pd_decode_enabled,
        )

        for mode in (
            Mode.LOCAL_OFFLOAD,
            Mode.PD_DECODE_OFFLOAD,
        ):
            with self.subTest(mode=mode):
                cache = self.make_cache(mode)
                self.assertEqual(sum(b.nbytes for b in cache.host_kv_buffer), 576)
                req = SimpleNamespace(
                    kv=SimpleNamespace(req_pool_idx=1),
                    origin_input_ids=[11, 22],
                    bootstrap_room=42,
                )
                cache.init_req(req)
                cache.record_pd_request_metadata(req)
                self.assertEqual(
                    is_sparse_pd_decode_enabled(cache), mode.uses_pd_decode_staging
                )
                if mode.uses_pd_decode_staging:
                    staging = SparsePDDecodeStagingPool(cache, page_size=4)
                    indices = staging.rewrite_dst_indices(
                        42, np.array([7], dtype=np.int32)
                    )
                    self.assertEqual(indices.tolist(), [0])
                    for layer in range(cache.layer_num):
                        cache.pd_decode_k_staging[layer][0, :2].fill_(11)
                        cache.pd_decode_v_staging[layer][0, :2].fill_(22)
                    staging.offload_room_to_host(42)
                    self.assertEqual(staging.available_slots(), 1)
                    self.assertEqual(cache._pd_room_to_req_pool_idx, {})
                else:
                    cache.host_kv_buffer[0][1, :2, :, :2] = 11
                    cache.host_kv_buffer[0][1, :2, :, 2:] = 22
                batch = SimpleNamespace(
                    req_pool_indices=torch.tensor([1]),
                    seq_lens=torch.tensor([3]),
                    out_cache_loc=torch.tensor([7]),
                    forward_mode=SimpleNamespace(is_decode=lambda: True),
                )
                k = torch.full((1, 1, 2), 33, dtype=torch.bfloat16)
                rope = torch.full_like(k, 44)
                cache.offload_v2(
                    k, rope, SimpleNamespace(layer_id=5), batch, CPUStream()
                )
                actual_k, actual_rope = cache.get_forward_kv(5, batch)
                self.assertEqual(actual_k[:, 0, 0].tolist(), [11, 11, 33])
                self.assertEqual(actual_rope[:, 0, 0].tolist(), [22, 22, 44])
                # v1 remains callable for callers supplying full native views.
                batch.out_cache_loc.fill_(0)
                cache.offload(
                    k + 1, rope + 1, SimpleNamespace(layer_id=5), batch, CPUStream()
                )
                actual_k, actual_rope = cache.get_forward_kv(5, batch)
                self.assertEqual(actual_k[-1, 0, 0].item(), 34)
                self.assertEqual(actual_rope[-1, 0, 0].item(), 45)
                self.rows.clear()
                self.assertTrue((cache.host_kv_ctx_len == 0).all())

    def test_formal_attention_rejects_extend_before_any_host_prefix_read(self):
        with patch.dict(sys.modules, {"torch_npu": ModuleType("torch_npu")}):
            attention = importlib.import_module("ascend_sparse.attention")
        backend = SimpleNamespace(
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            sparse_kv_manager=self.make_cache(Mode.PD_DECODE_MEMPOOL),
            mempool_runtime=object(),
        )
        batch = SimpleNamespace(
            forward_mode=SimpleNamespace(
                is_decode=lambda: False, is_extend_without_speculative=lambda: True
            )
        )
        kv = torch.zeros((1, 2))
        with self.assertRaisesRegex(RuntimeError, "only supports decode"):
            attention.forward_sparsity_driven_kv_offload(
                backend,
                kv,
                kv,
                kv,
                SimpleNamespace(tp_k_head_num=1),
                batch,
                q_rope=kv,
                k_rope=kv,
                topk_indices=torch.zeros((1, 1)),
            )

    def test_resource_gate_host_roundtrip_uses_real_materialization(self):
        # Check the hardware gate's data oracle on CPU too. Only SDK operations
        # are substituted; the gate's write/read/hit assertions are unchanged.
        scripts = str(Path(__file__).resolve().parents[2] / "scripts")
        with patch.object(sys, "path", [scripts, *sys.path]):
            gate = importlib.import_module("verify_resources")
        self.native.kv_lora_rank = 512
        self.native.qk_rope_head_dim = 64
        cache = self.module.SparseKVCacheManager(
            self.rows, self.allocator, sparse_context_len=2048, mode=Mode.LOCAL_OFFLOAD
        )
        req = SimpleNamespace(kv=SimpleNamespace(req_pool_idx=1))
        gate.check_host_roundtrip(cache, req, torch.tensor([7]), torch)

    def test_pd_adapter_preserves_native_indices_when_staging_is_disabled(self):
        import numpy as np

        from ascend_mempool_pd.sparse_pd import (
            SparsePDDecodeStagingPool,
            is_sparse_pd_decode_enabled,
        )

        # Use the same method loader as the existing native-release tests. Only
        # the parent transport and device imports are substituted on this host.
        environ = ModuleType("sglang.srt.environ")
        environ.envs = SimpleNamespace(
            SGLANG_NPU_ENABLE_MEMPOOL=SimpleNamespace(get=lambda: False)
        )
        self.enterContext(patch.dict(sys.modules, {environ.__name__: environ}))
        for mode in (
            Mode.PD_DECODE_MEMPOOL,
            Mode.PD_DECODE_OFFLOAD,
        ):
            with self.subTest(mode=mode):
                cache = self.make_cache(mode)
                self.native.get_state_layer_ids = lambda: [5, 6]
                req = SimpleNamespace(
                    kv=SimpleNamespace(req_pool_idx=1),
                    origin_input_ids=[11, 22],
                    bootstrap_room=42,
                )
                cache.record_pd_request_metadata(req)
                effects = []

                class TransportManager:
                    def __init__(self, *args):
                        pass

                    def update_status(self, room, status):
                        effects.append((room, status))

                class TransportReceiver:
                    def send_metadata(self, *args, **kwargs):
                        return args, kwargs

                namespace = dict(
                    MooncakeKVManager=TransportManager,
                    MooncakeKVReceiver=TransportReceiver,
                    MempoolFrameRouter=lambda: None,
                    get_sparse_pd_manager=lambda: cache,
                    is_sparse_pd_decode_enabled=is_sparse_pd_decode_enabled,
                    SparsePDDecodeStagingPool=SparsePDDecodeStagingPool,
                    KVPoll=SimpleNamespace(Success="success", Failed="failed"),
                    logger=logging.getLogger(__name__),
                    np=np,
                )
                manager_cls = load_methods(
                    "disaggregation/ascend/conn.py",
                    "AscendKVManager",
                    {"__init__", "update_status"},
                    namespace,
                )
                receiver_cls = load_methods(
                    "disaggregation/ascend/conn.py",
                    "AscendKVReceiver",
                    {"send_metadata"},
                    namespace,
                )
                entries = 6 if mode.uses_pd_decode_staging else 2
                args = SimpleNamespace(
                    page_size=4,
                    kv_data_ptrs=[0] * entries,
                    kv_data_lens=[0] * entries,
                    kv_item_lens=[0] * entries,
                )
                adapter = manager_cls(args, "decode", SimpleNamespace())
                receiver = receiver_cls()
                receiver.kv_mgr = adapter
                receiver.bootstrap_infos = object()
                receiver.bootstrap_room = 42
                sent, kwargs = receiver.send_metadata(np.array([7], dtype=np.int32))
                if mode.uses_pd_decode_staging:
                    self.assertIsNotNone(adapter.sparse_pd_decode_staging)
                    self.assertEqual(sent[0].tolist(), [0])
                    self.assertEqual(kwargs["device_kv_indices"].tolist(), [7])
                    for k, v in zip(
                        cache.pd_decode_k_staging, cache.pd_decode_v_staging
                    ):
                        k[0, :2].fill_(11)
                        v[0, :2].fill_(22)
                else:
                    self.assertIsNone(adapter.sparse_pd_decode_staging)
                    self.assertEqual(sent[0].tolist(), [7])
                    self.assertIsNone(kwargs["device_kv_indices"])
                adapter.update_status(42, "success")
                self.assertEqual(effects, [(42, "success")])
                if mode.uses_pd_decode_staging:
                    self.assertEqual(
                        cache.host_kv_buffer[0][1, 0, 0].tolist(), [11, 11, 22, 22]
                    )
                else:
                    self.assertEqual(cache.host_kv_buffer, [])
                    self.assertIsNone(cache.pd_decode_k_staging)


if __name__ == "__main__":
    unittest.main()
