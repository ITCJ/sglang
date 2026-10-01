"""Check model-derived MLA capacity without importing an SGLang server."""

import unittest

from ascend_mempool.config import MempoolConfig


class TestMempoolConfig(unittest.TestCase):
    """Exercise the configuration-to-layout boundary."""

    def test_actual_mla_dimensions_and_asymmetric_capacities(self):
        """Allocate all model layers with separate prompt/decode token limits."""
        config = MempoolConfig(prefill_capacity=8192, decode_capacity=16384)
        layout = config.make_mla_layout(
            num_layers=78, kv_lora_rank=512, qk_rope_head_dim=64
        )
        self.assertEqual(layout.prompt.shape, (16, 8192, 1, 576))
        self.assertEqual(layout.decode.shape, (16, 16384, 1, 576))
        self.assertEqual(layout.prompt.layers, 78)
        self.assertEqual(layout.prompt.total_bytes, 11777605632)
        self.assertEqual(layout.contribution_bytes(0), 11811160064)
        self.assertEqual(layout.contribution_bytes(1), 23622320128)
        self.assertEqual(layout.rank_stride_bytes, 23622320128)

    def test_service_config_rejects_unsupported_launch_before_bm(self):
        """P native mode is supported; incompatible serving modes are rejected."""
        from types import SimpleNamespace

        args = SimpleNamespace(
            device="npu",
            tp_size=16,
            dp_size=1,
            pp_size=1,
            attn_cp_size=1,
            disaggregation_mode="prefill",
            disaggregation_transfer_backend="ascend",
            disable_radix_cache=True,
            speculative_algorithm=None,
            disable_cuda_graph=True,
            optimistic_prefill_attempts=0,
            mempool_prefill_host="10.0.0.1",
            mempool_nic="tcp://10.0.0.1:24670",
        )
        config = MempoolConfig.from_server_args(
            args, sparse_enabled=True, mla=True, dtype="bfloat16", mlapo=False
        )
        self.assertEqual(config.prefill_host, "10.0.0.1")
        self.assertEqual(config.nic_for_rank(0), "tcp://10.0.0.1:24670")
        self.assertEqual(config.nic_for_rank(15), "tcp://10.0.0.1:24700")
        for key, value in (
            ("device", "cuda"),
            ("tp_size", 8),
            ("disaggregation_transfer_backend", "mooncake"),
            ("disable_radix_cache", False),
            ("speculative_algorithm", "EAGLE"),
            ("mempool_nic", "tcp://10.0.0.1:65520"),
            ("mempool_nic", "tcp://10.0.0.1"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                MempoolConfig.from_server_args(
                    SimpleNamespace(**(vars(args) | {key: value})),
                    sparse_enabled=True,
                    mla=True,
                    dtype="bfloat16",
                    mlapo=False,
                )
        with self.assertRaisesRegex(ValueError, "MLAPO"):
            MempoolConfig.from_server_args(
                args, sparse_enabled=True, mla=True, dtype="bfloat16", mlapo=True
            )
        with self.assertRaisesRegex(ValueError, "sparse"):
            MempoolConfig.from_server_args(
                args, sparse_enabled=False, mla=True, dtype="bfloat16", mlapo=False
            )

    def test_defaults_and_unsupported_model_storage(self):
        """Keep 16K defaults and reject non-BF16 or kernel-incompatible storage."""
        config = MempoolConfig()
        layout = config.make_mla_layout(
            num_layers=2, kv_lora_rank=512, qk_rope_head_dim=64
        )
        self.assertEqual(layout.prompt.shape, (16, 16384, 1, 576))
        self.assertEqual(layout.decode.shape, (16, 16384, 1, 576))
        for override in (
            {"num_layers": 0},
            {"kv_lora_rank": -1},
            {"qk_rope_head_dim": True},
            {"dtype": "float32"},
            {"kv_lora_rank": 16384},
        ):
            with self.subTest(override=override):
                dimensions = dict(num_layers=2, kv_lora_rank=512, qk_rope_head_dim=64)
                dimensions.update(override)
                with self.assertRaises(ValueError):
                    config.make_mla_layout(**dimensions)
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    MempoolConfig(prefill_capacity=capacity)
                with self.assertRaises(ValueError):
                    MempoolConfig(decode_capacity=capacity)
        with self.assertRaisesRegex(ValueError, "UINT32_MAX"):
            MempoolConfig(decode_capacity=1 << 31).make_mla_layout(
                num_layers=2, kv_lora_rank=512, qk_rope_head_dim=64
            )


if __name__ == "__main__":
    unittest.main()
