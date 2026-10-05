"""Check startup resource selection independently of NPU and BM allocation."""

import unittest
from types import SimpleNamespace

from ascend_sparse.config import (
    SparseKVOffloadMode,
    get_sparsity_driven_kv_offload_cell_size,
)


class TestSparseKVConfiguration(unittest.TestCase):
    def test_mempool_keeps_shadow_storage_until_cutover_is_implemented(self):
        """S1 must keep the accepted P/D transfer and host reference runnable."""
        mode = SparseKVOffloadMode.from_flags(
            sparse_enabled=True,
            mempool_enabled=True,
            disaggregation_mode="decode",
            transfer_backend="ascend",
            max_running_requests=4,
        )
        self.assertEqual(mode, SparseKVOffloadMode.PD_DECODE_MEMPOOL_SHADOW)
        self.assertTrue(mode.uses_sparse_kv_cache)
        self.assertTrue(mode.uses_host_kv_offload)
        self.assertTrue(mode.uses_pd_decode_staging)
        self.assertTrue(mode.uses_mempool_bm)

    def test_formal_decode_keeps_sparse_device_layout_without_host_storage(self):
        """Formal D still needs an Index K pool and HBM cache; P keeps native KV."""
        decode = SparseKVOffloadMode.PD_DECODE_MEMPOOL
        prefill = SparseKVOffloadMode.PD_PREFILL_MEMPOOL
        self.assertTrue(decode.uses_sparse_kv_cache)
        self.assertFalse(prefill.uses_sparse_kv_cache)
        for mode, expected_cell_size in ((decode, 19968), (prefill, None)):
            with self.subTest(mode=mode):
                self.assertFalse(mode.uses_host_kv_offload)
                self.assertFalse(mode.uses_pd_decode_staging)
                self.assertTrue(mode.uses_mempool_bm)
                self.assertEqual(
                    get_sparsity_driven_kv_offload_cell_size(
                        model_config=SimpleNamespace(index_head_dim=128),
                        use_mla_backend=True,
                        num_layers=78,
                        element_size=2,
                        mode=mode,
                    ),
                    expected_cell_size,
                )
                with self.assertRaisesRegex(ValueError, "S5"):
                    mode.validate_runtime_support()

    def test_startup_matrix_preserves_local_native_and_shadow_resources(self):
        """Turning BM off restores ordinary PD; P shadow keeps native storage."""
        cases = (
            (False, False, "null", "disabled", (False, False, False, False), None),
            (True, False, "null", "local_offload", (True, True, False, False), 19968),
            (
                True,
                False,
                "prefill",
                "pd_prefill_native",
                (False, False, False, False),
                None,
            ),
            (
                True,
                False,
                "decode",
                "pd_decode_offload",
                (True, True, True, False),
                19968,
            ),
            (
                True,
                True,
                "prefill",
                "pd_prefill_mempool_shadow",
                (False, False, False, True),
                None,
            ),
            (
                True,
                True,
                "decode",
                "pd_decode_mempool_shadow",
                (True, True, True, True),
                19968,
            ),
        )
        for sparse, mempool, role, name, capabilities, cell_size in cases:
            with self.subTest(role=role, sparse=sparse, mempool=mempool):
                mode = SparseKVOffloadMode.from_flags(
                    sparse_enabled=sparse,
                    mempool_enabled=mempool,
                    disaggregation_mode=role,
                    transfer_backend="ascend",
                    max_running_requests=4 if sparse else None,
                )
                mode.validate_runtime_support()
                self.assertEqual(mode.value, name)
                self.assertEqual(
                    (
                        mode.uses_sparse_kv_cache,
                        mode.uses_host_kv_offload,
                        mode.uses_pd_decode_staging,
                        mode.uses_mempool_bm,
                    ),
                    capabilities,
                )
                self.assertEqual(
                    get_sparsity_driven_kv_offload_cell_size(
                        model_config=SimpleNamespace(index_head_dim=128),
                        use_mla_backend=True,
                        num_layers=78,
                        element_size=2,
                        mode=mode,
                    ),
                    cell_size,
                )

    def test_invalid_launch_is_rejected_without_device_or_bm_dependencies(self):
        """Reject inconsistent feature flags, missing bounds and unsupported PD."""
        valid = dict(
            sparse_enabled=True,
            mempool_enabled=True,
            disaggregation_mode="decode",
            transfer_backend="ascend",
            max_running_requests=4,
        )
        for override, error in (
            ({"sparse_enabled": False}, "requires sparse"),
            ({"disaggregation_mode": "null"}, "PD role"),
            ({"disaggregation_mode": "invalid"}, "PD role"),
            ({"transfer_backend": "mooncake"}, "ascend"),
            ({"max_running_requests": None}, "max_running_requests"),
            ({"max_running_requests": 0}, "max_running_requests"),
        ):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(ValueError, error),
            ):
                SparseKVOffloadMode.from_flags(**(valid | override))


if __name__ == "__main__":
    unittest.main()
