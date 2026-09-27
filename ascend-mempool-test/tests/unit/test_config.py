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
