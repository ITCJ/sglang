"""Fixed HBM reservation is separate from the attention selection width."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from ascend_sparse import config


def load_env():
    path = Path(__file__).resolve().parents[3] / "python/sglang/srt/environ.py"
    spec = importlib.util.spec_from_file_location("sglang.srt.environ", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLRUBudget(unittest.TestCase):
    def test_capacity_and_fixed_reservation_include_metadata_and_staging(self):
        with patch.dict(sys.modules, {"sglang.srt.environ": load_env()}):
            for factor, expected in (
                (1, 1942848),
                (2, 3179840),
                (3, 4416832),
                (4, 5653824),
            ):
                with (
                    self.subTest(factor=factor),
                    patch.dict(
                        os.environ,
                        {"SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR": str(factor)},
                    ),
                ):
                    capacity = (
                        config.get_sparsity_driven_kv_offload_device_cache_capacity(
                            sparse_context_len=2048, enable_lru=True
                        )
                    )
                    self.assertEqual(capacity, factor * 2048)
                    kwargs = dict(
                        request_rows=5,
                        max_context_len=4100,
                        sparse_context_len=2048,
                        device_cache_capacity=capacity,
                        enable_lru=True,
                        kv_head_dim=4,
                        num_layers=6,
                        element_size=2,
                        page_size=128,
                    )
                    actual = config.get_sparsity_driven_kv_offload_fixed_memory_size(
                        mode=config.SparseKVOffloadMode.PD_DECODE_MEMPOOL, **kwargs
                    )
                    self.assertEqual(actual, expected)
                    staging = config.get_sparsity_driven_kv_offload_fixed_memory_size(
                        mode=config.SparseKVOffloadMode.PD_DECODE_OFFLOAD, **kwargs
                    )
                    self.assertEqual(staging - actual, 202752)
            self.assertEqual(
                config.get_sparsity_driven_kv_offload_device_cache_capacity(
                    sparse_context_len=17, enable_lru=False
                ),
                17,
            )


if __name__ == "__main__":
    unittest.main()
