import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sglang.srt.disaggregation.ascend.sparse_pd import is_sparse_pd_decode_enabled
from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
    SparseKVOffloadMode,
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
    )


class TestSparsityDrivenKVOffloadConfig(unittest.TestCase):
    def setUp(self):
        self.model_config = _make_glm51_model_config()
        self.disagg = SimpleNamespace(
            disaggregation_mode="null",
            disaggregation_transfer_backend="ascend",
        )
        for patcher in (
            patch.dict(os.environ, {"SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD": "1"}),
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.is_npu",
                return_value=True,
            ),
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.attention_backends",
                return_value=("ascend", "ascend"),
            ),
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.get_schedule",
                return_value=SimpleNamespace(max_running_requests=8),
            ),
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.get_disagg",
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
                self.assertEqual(
                    mode.uses_host_kv_offload, role in ("null", "decode")
                )
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
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.get_disagg",
                side_effect=AssertionError("runtime config should not be read"),
            ),
        ):
            self.assertIs(self.resolve(), SparseKVOffloadMode.DISABLED)
            self.assertIsNone(self.cell_size())

    def test_pool_can_resolve_from_published_process_config(self):
        with (
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.process_model_config",
                return_value=self.model_config,
            ),
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.uses_mla_backend",
                return_value=True,
            ),
        ):
            self.assertIs(
                resolve_sparse_kv_offload_mode(),
                SparseKVOffloadMode.LOCAL_OFFLOAD,
            )

    def test_sparse_pd_connector_uses_resolved_mode(self):
        for mode in SparseKVOffloadMode:
            with (
                self.subTest(mode=mode),
                patch(
                    "sglang.srt.disaggregation.ascend.sparse_pd.resolve_sparse_kv_offload_mode",
                    return_value=mode,
                ),
            ):
                self.assertEqual(
                    is_sparse_pd_decode_enabled(object()),
                    mode is SparseKVOffloadMode.PD_DECODE_OFFLOAD,
                )

        with (
            patch(
                "sglang.srt.disaggregation.ascend.sparse_pd.get_sparse_pd_manager",
                return_value=None,
            ),
            patch(
                "sglang.srt.disaggregation.ascend.sparse_pd.resolve_sparse_kv_offload_mode",
                side_effect=AssertionError("mode should not be resolved"),
            ),
        ):
            self.assertFalse(is_sparse_pd_decode_enabled())

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
            "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.attention_backends",
            return_value=("ascend", "torch_native"),
        ):
            with self.assertRaisesRegex(ValueError, "Ascend MLA attention backend"):
                self.resolve()

    def test_missing_request_capacity_rejects_sparse_kv_offload(self):
        with patch(
            "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.get_schedule",
            return_value=SimpleNamespace(max_running_requests=None),
        ):
            with self.assertRaisesRegex(ValueError, "max_running_requests"):
                self.resolve()

    def test_non_npu_and_non_mla_reject_sparse_kv_offload(self):
        with patch(
            "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.is_npu",
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
