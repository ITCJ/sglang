"""Index K layout and capacity at the Ascend pool/configuration boundary.

Execute production configuration functions without importing the serving stack.
Only published runtime settings and device capability detection are substituted.
"""

import abc
import logging
import sys
import unittest
from contextlib import nullcontext
from dataclasses import dataclass
from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import patch

import torch
from test_native_release import load_methods
from test_pd_transfer import load_functions

from ascend_sparse import config
from ascend_sparse.config import SparseKVOffloadMode as Mode

PRODUCERS = (
    0,
    1,
    2,
    6,
    10,
    14,
    18,
    22,
    26,
    30,
    34,
    38,
    42,
    46,
    50,
    54,
    58,
    62,
    66,
    70,
    74,
)


class TestIndexerLayout(unittest.TestCase):
    def setUp(self):
        self.hf = NS(
            num_hidden_layers=78,
            indexer_types=["full" if i in PRODUCERS else "shared" for i in range(78)],
            index_topk_freq=99,  # Explicit types must win over this fallback.
        )
        self.kvc = NS(
            model_config=NS(
                hf_config=self.hf,
                index_head_dim=128,
                kv_lora_rank=512,
                qk_rope_head_dim=64,
            ),
            layer_info=NS(start_layer=0, end_layer=78, num_effective_layers=78),
            sparse_kv_offload_mode=Mode.PD_DECODE_MEMPOOL,
            kv_cache_dtype=torch.bfloat16,
            use_mla_backend=True,
            is_draft_worker=False,
            spec_algorithm=NS(is_none=lambda: True),
            pool_page_size=4,
            device="cpu",
            server_args=NS(enable_hisparse=False),
        )
        self.memory = NS(
            enable_hisparse=False,
            enable_hierarchical_cache=False,
            enable_unified_cache_external_linker=False,
        )
        self.parallel = NS(pp_size=1, attn_cp_size=1, attn_dcp_size=1, attn_tp_size=16)
        runtime = ModuleType("sglang.srt.runtime_context")
        runtime.get_memory = lambda: self.memory
        runtime.get_parallel = lambda: self.parallel
        runtime.get_disagg = lambda: NS(disaggregation_mode="prefill")
        runtime.get_exec = lambda: NS(features=NS(enable_memory_saver=False))
        model = ModuleType("sglang.srt.configs.model_config")
        model.is_deepseek_dsa = lambda cfg: True
        load_functions(
            "configs/model_config.py", {"dsa_layer_skips_topk"}, model.__dict__
        )
        modules = patch.dict(sys.modules, {m.__name__: m for m in (runtime, model)})
        modules.start()
        self.addCleanup(modules.stop)
        self.model_module = model
        self.runtime_module = runtime

    def build_pool_and_price(self):
        """Real NPU pool allocation on CPU, without device library imports."""
        namespace = dict(
            torch=torch,
            abc=abc,
            dataclass=dataclass,
            logger=logging.getLogger(__name__),
            GB=1 << 30,
            TorchMemorySaverAdapter=NS(
                create=lambda **kw: NS(region=lambda *a: nullcontext())
            ),
            maybe_init_custom_mem_pool=lambda **kw: (False, None, None),
            GPU_MEMORY_TYPE_KV_CACHE="kv_cache",
            get_bool_env_var=lambda name: False,
            get_tensor_size_bytes=lambda t: t.nbytes,
            SparseKVOffloadMode=Mode,
            _is_npu=True,
            get_dsa_index_head_dim=lambda cfg: 128,
            DSATokenToKVPool=NS(
                quant_block_size=128, index_k_with_scale_buffer_dtype=torch.uint8
            ),
            **{
                n: getattr(self.runtime_module, n)
                for n in ("get_memory", "get_parallel", "get_exec", "get_disagg")
            },
            dsa_layer_skips_topk=self.model_module.dsa_layer_skips_topk,
        )
        cache = load_methods(
            "mem_cache/memory_pool.py",
            "KVCache",
            {"__init__", "_finalize_allocation_log"},
            namespace,
            standalone=True,
        )
        cache.post_capture_active = False
        namespace["MLATokenToKVPool"] = type("MLATokenToKVPool", (cache,), {})
        pool = load_methods(
            "hardware_backend/npu/memory_pool_npu.py",
            "NPUMLATokenToKVPool",
            {
                "__init__",
                "get_kv_size_bytes",
                "get_state_layer_ids",
                "get_state_buf_infos",
                "get_index_k_buffer",
                "_get_indexer_slot",
                "get_kv_layer_ids",
                "get_contiguous_buf_infos",
                "_raise_if_native_kv_cache_disabled",
            },
            namespace,
        )
        pool_module = ModuleType("sglang.srt.hardware_backend.npu.memory_pool_npu")
        pool_module.NPUMLATokenToKVPool = pool
        utils = ModuleType("sglang.srt.hardware_backend.npu.utils")
        utils.is_npu_arch35 = lambda: False
        kvc_module = ModuleType("sglang.srt.mem_cache.kv_cache_configurator")
        kvc_module.__dict__.update(namespace)
        load_functions(
            "mem_cache/kv_cache_configurator.py",
            {"_should_elide_dsa_index_k"},
            kvc_module.__dict__,
        )
        namespace["_should_elide_dsa_index_k"] = kvc_module._should_elide_dsa_index_k
        factory = load_methods(
            "mem_cache/kv_cache_configurator.py",
            "KVCacheConfigurator",
            {"_build_ascend_mla_kv_pool"},
            namespace,
            standalone=True,
        )
        price = load_methods(
            "model_executor/pool_configurator.py",
            "DefaultPoolConfigurator",
            {"_compute_dsa_indexer_cell_size"},
            namespace,
            standalone=True,
        )
        with patch.dict(
            sys.modules,
            {
                pool_module.__name__: pool_module,
                utils.__name__: utils,
                "sglang.srt.mem_cache.kv_cache_configurator": kvc_module,
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config": config,
            },
        ):
            result = factory._build_ascend_mla_kv_pool(
                self.kvc, max_total_num_tokens=8, is_dsa_model=True
            )
            cost = price()._compute_dsa_indexer_cell_size(kvc=self.kvc, num_layers=78)
        return result, cost

    def test_production_builder_allocates_and_prices_the_same_21_layers(self):
        self.kvc.sparse_kv_offload_mode = Mode.PD_PREFILL_MEMPOOL
        pool, price = self.build_pool_and_price()
        self.assertEqual(pool.get_state_layer_ids(), list(PRODUCERS))
        self.assertEqual(pool.index_k_buffer.nbytes, 64512)  # 12 physical token rows.
        self.assertEqual(price, 5376)
        self.assertEqual(pool.k_buffer.shape[0], 78)
        self.assertEqual(pool.v_buffer.shape[0], 78)
        pool.get_index_k_buffer(74).fill_(74)
        self.assertTrue((pool.index_k_buffer[20] == 74).all())
        self.assertEqual(len(pool.get_contiguous_buf_infos()[0]), 21)

    def test_sparse_pd_prices_only_producers_and_keeps_model_layers(self):
        for mode in (
            Mode.PD_PREFILL_NATIVE,
            Mode.PD_DECODE_OFFLOAD,
            Mode.PD_PREFILL_MEMPOOL,
            Mode.PD_DECODE_MEMPOOL,
        ):
            with self.subTest(mode=mode):
                self.kvc.sparse_kv_offload_mode = mode
                ids = config.get_sparse_pd_indexer_layer_ids(self.kvc)
                self.assertEqual(ids, PRODUCERS)
                self.assertEqual(self.kvc.layer_info.end_layer, 78)
                size = config.get_sparsity_driven_kv_offload_cell_size(
                    model_config=self.kvc.model_config,
                    use_mla_backend=True,
                    num_layers=78,
                    element_size=2,
                    mode=mode,
                    indexer_layer_ids=ids,
                )
                self.assertEqual(size, 5376 if mode.uses_sparse_kv_cache else None)

    def test_decode_early_cell_size_path_matches_actual_compact_allocation(self):
        namespace = dict(torch=torch, _is_npu=True, get_parallel=lambda: self.parallel)
        price = load_methods(
            "model_executor/pool_configurator.py",
            "DefaultPoolConfigurator",
            {"_compute_cell_size"},
            namespace,
            standalone=True,
        )
        cp = ModuleType("sglang.srt.layers.cp.utils")
        cp.get_glm_dsa_layer_split_effective_num_layers = lambda kvc, layers: layers
        with patch.dict(
            sys.modules,
            {
                cp.__name__: cp,
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config": config,
            },
        ):
            for mode in (Mode.PD_DECODE_OFFLOAD, Mode.PD_DECODE_MEMPOOL):
                with self.subTest(mode=mode):
                    self.kvc.sparse_kv_offload_mode = mode
                    pool, _ = self.build_pool_and_price()
                    self.assertIsNone(pool.k_buffer)
                    self.assertEqual(pool.num_indexer_layers, 21)
                    self.assertEqual(price()._compute_cell_size(self.kvc, 78), 5376)

    def test_invalid_sharing_is_rejected_before_allocation(self):
        for types in (
            ["full"],
            ["unknown"] + ["full"] * 77,
            ["shared"] + ["full"] * 77,
            "full",
        ):
            with (
                self.subTest(types=types),
                self.assertRaisesRegex(ValueError, "indexer_types|producer"),
            ):
                self.hf.indexer_types = types
                config.get_sparse_pd_indexer_layer_ids(self.kvc)

    def test_legacy_glm51_keeps_every_indexer_layer(self):
        self.hf.indexer_types = None
        self.hf.index_topk_freq = 1
        self.assertEqual(
            config.get_sparse_pd_indexer_layer_ids(self.kvc), tuple(range(78))
        )

    def test_unsupported_paths_keep_the_existing_layout(self):
        for obj, name, value in (
            (self.kvc, "sparse_kv_offload_mode", Mode.DISABLED),
            (self.kvc, "sparse_kv_offload_mode", Mode.LOCAL_OFFLOAD),
            (self.kvc, "kv_cache_dtype", torch.float8_e4m3fn),
            (self.kvc, "is_draft_worker", True),
            (self.kvc, "spec_algorithm", NS(is_none=lambda: False)),
            (self.memory, "enable_hisparse", True),
            (self.memory, "enable_hierarchical_cache", True),
            (self.memory, "enable_unified_cache_external_linker", True),
            (self.parallel, "pp_size", 2),
            (self.parallel, "attn_cp_size", 2),
            (self.parallel, "attn_dcp_size", 2),
        ):
            with self.subTest(name=name, value=value), patch.object(obj, name, value):
                self.assertIsNone(config.get_sparse_pd_indexer_layer_ids(self.kvc))

    def test_shared_layer_without_current_producer_fails_before_device_work(self):
        namespace = {"torch": torch}
        load_functions(
            "hardware_backend/npu/modules/deepseek_v2_attention_mla_npu.py",
            {"forward_dsa_prepare_npu"},
            namespace,
        )
        with self.assertRaisesRegex(ValueError, "layer 3.*producer"):
            namespace["forward_dsa_prepare_npu"](
                NS(skip_topk=True, is_nextn=False, layer_id=3),
                torch.tensor([5]),
                torch.zeros((1, 8)),
                NS(forward_mode=NS(is_idle=lambda: False)),
                None,
                None,
                prev_topk_indices=None,
            )


if __name__ == "__main__":
    unittest.main()
