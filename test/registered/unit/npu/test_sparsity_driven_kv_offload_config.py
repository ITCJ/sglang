import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
    get_sparsity_driven_kv_offload_cell_size,
    get_sparsity_driven_kv_offload_fixed_memory_size,
    get_sparsity_driven_kv_offload_sparse_context_len,
    is_sparsity_driven_kv_offload_enabled,
)
from sglang.test.ci.ci_register import register_cpu_ci

register_cpu_ci(est_time=1, suite="base-a-test-cpu")


def _make_glm51_model_config():
    hf_config = SimpleNamespace(
        architectures=["GlmMoeDsaForCausalLM"],
        index_head_dim=128,
        index_topk=2048,
    )
    hf_config.get_text_config = lambda: hf_config
    return SimpleNamespace(
        hf_config=hf_config,
        index_head_dim=128,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
    )


class TestSparsityDrivenKVOffloadConfig(unittest.TestCase):
    def test_glm_dsa_model_enables_sparse_kv_offload(self):
        server_args = SimpleNamespace(
            attention_backend="ascend",
            max_running_requests=8,
        )

        with (
            patch.dict(
                os.environ,
                {"SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD": "1"},
            ),
            patch(
                "sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config.is_npu",
                return_value=True,
            ),
        ):
            model_config = _make_glm51_model_config()

            self.assertTrue(
                is_sparsity_driven_kv_offload_enabled(
                    model_config=model_config,
                    server_args=server_args,
                    use_mla_backend=True,
                )
            )
            self.assertEqual(
                get_sparsity_driven_kv_offload_sparse_context_len(
                    model_config=model_config
                ),
                2048,
            )
            self.assertEqual(
                get_sparsity_driven_kv_offload_cell_size(
                    model_config=model_config,
                    server_args=server_args,
                    use_mla_backend=True,
                    num_layers=2,
                    element_size=2,
                ),
                512,
            )
            self.assertEqual(
                get_sparsity_driven_kv_offload_fixed_memory_size(
                    model_config=model_config,
                    server_args=server_args,
                    use_mla_backend=True,
                    num_layers=2,
                    element_size=2,
                    max_running_requests_per_worker=8,
                ),
                (8 + 1) * 4096 * (512 + 64) * 2 * 2,
            )


if __name__ == "__main__":
    unittest.main()
