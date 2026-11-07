"""Budget hook and delivery entry checks at their existing public seams."""

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import torch
from test_lru_budget import load_env
from test_native_release import load_functions, load_methods

from ascend_sparse import config


class TestLRUIntegration(unittest.TestCase):
    def test_budget_charges_padding_receive_rows_and_upstream_scratch_once(self):
        schedule = SimpleNamespace(max_running_requests=4, page_size=128)
        runtime = ModuleType("sglang.srt.runtime_context")
        runtime.get_schedule = lambda: schedule
        runtime.get_disagg = lambda: SimpleNamespace(
            disaggregation_mode="decode", disaggregation_decode_extra_slots=2
        )
        sizing = ModuleType("sglang.srt.mem_cache.allocation_sizing")
        sizing.get_req_to_token_extra_context_len = lambda: 4
        pool_module = ModuleType("sglang.srt.model_executor.pool_configurator")
        budgets = []

        def calculate(budget, page):
            budgets.append(budget)
            return SimpleNamespace(
                max_total_num_tokens=((budget - 128) // 128) // page * page
            )

        pool_module.create_memory_pool_configurator = lambda _: SimpleNamespace(
            calculate_pool_sizes=calculate
        )
        env = load_env()
        namespace = dict(
            torch=torch,
            envs=env.envs,
            get_sparsity_driven_kv_offload_sparse_context_len=lambda **_: 2048,
            get_sparsity_driven_kv_offload_device_cache_capacity=config.get_sparsity_driven_kv_offload_device_cache_capacity,
            get_sparsity_driven_kv_offload_fixed_memory_size=config.get_sparsity_driven_kv_offload_fixed_memory_size,
        )
        load_functions(
            "hardware_backend/npu/memory_pool_npu.py",
            {"adjust_sparse_kv_config"},
            namespace,
        )
        adapter = ModuleType("sglang.srt.hardware_backend.npu.memory_pool_npu")
        adapter.adjust_sparse_kv_config = namespace["adjust_sparse_kv_config"]
        scope = dict(
            dataclass=lambda **kwargs: (
                lambda cls: cls
            ),  # Only load the method, not pool fields.
            _is_npu=True,
            get_schedule=lambda: schedule,
            get_memory=lambda: SimpleNamespace(enable_unified_memory=False),
        )
        cls = load_methods(
            "mem_cache/kv_cache_configurator.py",
            "KVCacheConfigurator",
            {"config_from_budget"},
            scope,
        )
        kvc = cls()
        kvc.sparse_kv_offload_mode = config.SparseKVOffloadMode.PD_DECODE_MEMPOOL
        kvc.ps = SimpleNamespace(attn_dp_size=2)
        kvc.model_config = SimpleNamespace(
            context_len=4096, kv_lora_rank=2, qk_rope_head_dim=2
        )
        kvc.layer_info = SimpleNamespace(num_effective_layers=6)
        kvc.kv_cache_dtype = torch.bfloat16
        kvc._apply_token_constraints = lambda n: n
        modules = {m.__name__: m for m in (runtime, sizing, pool_module, adapter, env)}
        with (
            patch.dict(sys.modules, modules),
            patch.dict(
                "os.environ",
                SGLANG_NPU_SPARSE_KV_ENABLE_LRU="1",
                SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR="2",
            ),
        ):
            result = kvc.config_from_budget(10000000)
            self.assertEqual(budgets, [10000000, 6820160])
            self.assertEqual(result.max_total_num_tokens, 53248)
            budgets.clear()
            scope["_is_npu"] = False
            kvc.config_from_budget(10000000)
            self.assertEqual(budgets, [10000000])

    def test_installed_multibase_is_required_without_library_fallback(self):
        from ascend_mempool.multibase import load_kernel

        module = ModuleType("sgl_kernel_npu.sparsity_driven_kv_offload")
        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch.object(torch.ops, "load_library") as standalone,
        ):
            with self.assertRaises(ImportError):
                load_kernel()
            installed = lambda *args: None
            module.unidex_copy_multibase = installed
            self.assertIs(load_kernel(), installed)
            standalone.assert_not_called()

    def test_service_comparison_requires_all_output_tokens(self):
        from run_ticket27 import compare_tokens

        ids = list(range(32))
        self.assertTrue(
            compare_tokens({"output_ids": ids}, {"output_ids": ids})["equal"]
        )
        for other in (ids[:-1], ids[:-1] + [99]):
            with self.assertRaises(ValueError):
                compare_tokens({"output_ids": ids}, {"output_ids": other})


if __name__ == "__main__":
    unittest.main()
