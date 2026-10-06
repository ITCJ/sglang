import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import torch

from sglang.srt.disaggregation.ascend.sparse_pd import is_sparse_pd_decode_enabled
from sglang.srt.hardware_backend.npu.memory_pool_npu import NPUMLATokenToKVPool
from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
    SparseKVOffloadMode,
    configure_for_model_runner,
    get_sparsity_driven_kv_offload_cell_size,
    get_sparsity_driven_kv_offload_sparse_context_len,
    resolve_sparse_kv_offload_mode,
)
from sglang.test.ci.ci_register import register_cpu_ci

register_cpu_ci(est_time=1, suite="base-a-test-cpu")


def _make_glm51_model_config():
    hf_config = SimpleNamespace(
        architectures=["GlmMoeDsaForCausalLM"],
        index_head_dim=128,
        index_topk=1536,
    )
    hf_config.get_text_config = lambda: hf_config
    return SimpleNamespace(
        hf_config=hf_config,
        index_head_dim=128,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
    )


class TestSparsityDrivenKVOffloadConfig(unittest.TestCase):
    def setUp(self):
        self.model_config = _make_glm51_model_config()
        self.disagg = SimpleNamespace(
            disaggregation_mode="null",
            disaggregation_transfer_backend="ascend",
        )
        for patcher in (
            patch.dict(
                os.environ,
                {
                    "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD": "1",
                    "SGLANG_NPU_ENABLE_MEMPOOL": "0",
                },
            ),
            patch(
                "sglang.srt.utils.common.is_npu",
                return_value=True,
            ),
            patch(
                "sglang.srt.runtime_context.attention_backends",
                return_value=("ascend", "ascend"),
            ),
            patch(
                "sglang.srt.runtime_context.get_schedule",
                return_value=SimpleNamespace(max_running_requests=8),
            ),
            patch(
                "sglang.srt.runtime_context.get_disagg",
                return_value=self.disagg,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def resolve(self):
        return resolve_sparse_kv_offload_mode(
            model_config=self.model_config,
            use_mla_backend=True,
        )

    def cell_size(self):
        return get_sparsity_driven_kv_offload_cell_size(
            model_config=self.model_config,
            use_mla_backend=True,
            num_layers=2,
            element_size=2,
        )

    def test_mode_and_device_capacity_by_role(self):
        for role, expected_mode, expected_cell_size in (
            ("null", SparseKVOffloadMode.LOCAL_OFFLOAD, 512),
            ("prefill", SparseKVOffloadMode.PD_PREFILL_NATIVE, None),
            ("decode", SparseKVOffloadMode.PD_DECODE_OFFLOAD, 512),
        ):
            with self.subTest(role=role):
                self.disagg.disaggregation_mode = role
                mode = self.resolve()
                self.assertIs(mode, expected_mode)
                self.assertEqual(self.cell_size(), expected_cell_size)
                self.assertEqual(mode.uses_host_kv_offload, role in ("null", "decode"))
                self.assertEqual(mode.uses_pd_decode_staging, role == "decode")

        self.assertEqual(
            get_sparsity_driven_kv_offload_sparse_context_len(
                model_config=self.model_config
            ),
            1536,
        )

    def test_disabled_mode_does_not_read_runtime_configuration(self):
        with (
            patch.dict(os.environ, {"SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD": "0"}),
            patch(
                "sglang.srt.runtime_context.get_disagg",
                side_effect=AssertionError("runtime config should not be read"),
            ),
        ):
            self.assertIs(self.resolve(), SparseKVOffloadMode.DISABLED)
            self.assertIsNone(self.cell_size())

    def test_pool_can_resolve_from_published_process_config(self):
        with (
            patch(
                "sglang.srt.runtime_context.process_model_config",
                return_value=self.model_config,
            ),
            patch(
                "sglang.srt.runtime_context.uses_mla_backend",
                return_value=True,
            ),
        ):
            self.assertIs(
                resolve_sparse_kv_offload_mode(),
                SparseKVOffloadMode.LOCAL_OFFLOAD,
            )

    def test_sparse_pd_connector_uses_resolved_mode(self):
        for mode in SparseKVOffloadMode:
            with self.subTest(mode=mode):
                self.assertEqual(
                    is_sparse_pd_decode_enabled(SimpleNamespace(mode=mode)),
                    mode is SparseKVOffloadMode.PD_DECODE_OFFLOAD,
                )

        with (
            patch(
                "sglang.srt.disaggregation.ascend.sparse_pd.get_sparse_pd_manager",
                return_value=None,
            ),
            patch(
                "sglang.srt.runtime_context.get_disagg",
                side_effect=AssertionError("mode should not be resolved"),
            ),
        ):
            self.assertFalse(is_sparse_pd_decode_enabled())

    def test_native_allocation_preserves_prefill_kv_and_decode_index_k(self):
        # CPU tensors exercise the real pool constructor without an NPU launch.
        for mode, native_kv in (
            (SparseKVOffloadMode.DISABLED, True),
            (SparseKVOffloadMode.LOCAL_OFFLOAD, False),
            (SparseKVOffloadMode.PD_PREFILL_NATIVE, True),
            (SparseKVOffloadMode.PD_DECODE_OFFLOAD, False),
            (SparseKVOffloadMode.PD_PREFILL_MEMPOOL, True),
            (SparseKVOffloadMode.PD_DECODE_MEMPOOL, False),
        ):
            with self.subTest(mode=mode):
                pool = NPUMLATokenToKVPool(
                    size=8,
                    page_size=4,
                    dtype=torch.bfloat16,
                    kv_lora_rank=512,
                    qk_rope_head_dim=64,
                    layer_num=2,
                    device="cpu",
                    enable_memory_saver=False,
                    index_head_dim=128,
                    sparse_kv_offload_mode=mode,
                )
                self.assertIs(pool.sparse_kv_offload_mode, mode)
                self.assertEqual(pool.k_buffer is not None, native_kv)
                self.assertEqual(pool.v_buffer is not None, native_kv)
                self.assertEqual(pool.get_index_k_buffer(0).shape, (3, 4, 1, 128))

    def make_runner(self, **arg_overrides):
        args = dict(
            device="npu",
            tp_size=16,
            dp_size=1,
            pp_size=1,
            attn_cp_size=1,
            disaggregation_mode=self.disagg.disaggregation_mode,
            disaggregation_transfer_backend="ascend",
            disable_radix_cache=True,
            disable_cuda_graph=True,
            mempool_prefill_host="10.0.0.1",
            mempool_nic="tcp://10.0.0.1:25670",
            mempool_prefill_capacity=512,
            mempool_decode_capacity=512,
        )
        return SimpleNamespace(
            server_args=SimpleNamespace(**(args | arg_overrides)),
            model_config=self.model_config,
            use_mla_backend=True,
            kv_cache_dtype=torch.bfloat16,
            layer_info=SimpleNamespace(start_layer=0, end_layer=78),
        )

    def test_startup_validates_before_any_pool_backend_or_runtime_exists(self):
        self.disagg.disaggregation_mode = "decode"
        with (
            patch.dict(os.environ, {"SGLANG_NPU_ENABLE_MEMPOOL": "1"}),
            patch(
                "sglang.srt.hardware_backend.npu.attention.mla_preprocess.is_mla_preprocess_enabled",
                return_value=False,
            ),
        ):
            for role, expected_mode in (
                ("prefill", SparseKVOffloadMode.PD_PREFILL_MEMPOOL),
                ("decode", SparseKVOffloadMode.PD_DECODE_MEMPOOL),
            ):
                with self.subTest(role=role):
                    self.disagg.disaggregation_mode = role
                    runner = self.make_runner()
                    configure_for_model_runner(runner)
                    self.assertIs(runner.sparse_kv_offload_mode, expected_mode)
                    self.assertEqual(runner.mempool_config.prefill_capacity, 512)
                    self.assertEqual(runner.mempool_config.decode_capacity, 512)

            runner = self.make_runner(tp_size=8)
            with self.assertRaisesRegex(ValueError, "tp_size"):
                configure_for_model_runner(runner)
            self.assertFalse(hasattr(runner, "mempool_config"))

            runner = self.make_runner(mempool_decode_capacity=1 << 31)
            with self.assertRaisesRegex(ValueError, "UINT32_MAX"):
                configure_for_model_runner(runner)

    def test_ordinary_startup_does_not_require_mempool_arguments(self):
        runner = self.make_runner()
        runner.server_args = SimpleNamespace()
        configure_for_model_runner(runner)
        self.assertIs(runner.sparse_kv_offload_mode, SparseKVOffloadMode.LOCAL_OFFLOAD)
        self.assertIsNone(runner.mempool_config)

    def test_pd_requires_ascend_transfer_backend(self):
        self.disagg.disaggregation_transfer_backend = "mooncake"
        for role in ("prefill", "decode"):
            with self.subTest(role=role):
                self.disagg.disaggregation_mode = role
                with self.assertRaisesRegex(
                    ValueError, "disaggregation_transfer_backend='ascend'"
                ):
                    self.resolve()
                with self.assertRaisesRegex(
                    ValueError, "disaggregation_transfer_backend='ascend'"
                ):
                    self.cell_size()

    def test_split_attention_backend_rejects_sparse_kv_offload(self):
        with patch(
            "sglang.srt.runtime_context.attention_backends",
            return_value=("ascend", "torch_native"),
        ):
            with self.assertRaisesRegex(ValueError, "Ascend MLA attention backend"):
                self.resolve()

    def test_missing_request_capacity_rejects_sparse_kv_offload(self):
        with patch(
            "sglang.srt.runtime_context.get_schedule",
            return_value=SimpleNamespace(max_running_requests=None),
        ):
            with self.assertRaisesRegex(ValueError, "max_running_requests"):
                self.resolve()

    def test_non_npu_and_non_mla_reject_sparse_kv_offload(self):
        with patch(
            "sglang.srt.utils.common.is_npu",
            return_value=False,
        ):
            with self.assertRaisesRegex(ValueError, "NPU DSA-family MLA"):
                self.resolve()
        with self.assertRaisesRegex(ValueError, "NPU DSA-family MLA"):
            resolve_sparse_kv_offload_mode(
                model_config=self.model_config,
                use_mla_backend=False,
            )
        self.model_config.hf_config.architectures = ["LlamaForCausalLM"]
        with self.assertRaisesRegex(ValueError, "NPU DSA-family MLA"):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
