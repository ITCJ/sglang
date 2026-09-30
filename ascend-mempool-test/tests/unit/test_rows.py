"""Check forward row coordinates against hand-worked MLA examples."""

import unittest

import torch

from ascend_mempool.rows import derive_kv_rows


class TestKVRows(unittest.TestCase):
    """Exercise layout inference without importing SGLang or torch_npu."""

    def test_decode_masks_dummy_cache_locations_and_context_bounds(self):
        """One compact row belongs to each request, including graph padding."""
        req, pos, valid = derive_kv_rows(
            5,
            is_decode=True,
            max_context_len=8,
            req_pool_indices=torch.tensor([3, 4, 0, 7, 9]),
            seq_lens=torch.tensor([5, 1, 1, 9, 3]),
            out_cache_loc=torch.tensor([12, 13, 0, 14, -1]),
        )
        self.assertEqual(req.tolist(), [3, 4, 0, 7, 9])
        self.assertEqual(pos.tolist(), [4, 0, 0, 8, 2])
        self.assertEqual(valid.tolist(), [True, False, False, False, False])

    def test_ragged_prefill_uses_each_requests_chunk_prefix(self):
        """Two then three rows have independently offset full token positions."""
        req, pos, valid = derive_kv_rows(
            5,
            is_decode=False,
            max_context_len=16,
            req_pool_indices=torch.tensor([2, 5]),
            extend_seq_lens=torch.tensor([2, 3]),
            extend_prefix_lens=torch.tensor([4, 7]),
            extend_seq_lens_cpu=[2, 3],
            out_cache_loc=torch.tensor([10, 11, 12, -1, 14]),
        )
        self.assertEqual(req.tolist(), [2, 2, 5, 5, 5])
        self.assertEqual(pos.tolist(), [4, 5, 7, 8, 9])
        self.assertEqual(valid.tolist(), [True, True, True, False, True])

    def test_moe_tail_padding_is_not_assigned_to_any_request(self):
        """Compact tokens precede global padding even when rows divide batch size."""
        req, pos, valid = derive_kv_rows(
            6,
            is_decode=False,
            max_context_len=16,
            req_pool_indices=torch.tensor([2, 5]),
            extend_seq_lens=torch.tensor([2, 1]),
            extend_prefix_lens=torch.tensor([4, 7]),
            extend_seq_lens_cpu=[2, 1],
            global_num_token_non_padded_cpu=3,
        )
        self.assertEqual(req.tolist(), [2, 2, 5, -1, -1, -1])
        self.assertEqual(pos.tolist(), [4, 5, 7, 0, 0, 0])
        self.assertEqual(valid.tolist(), [True, True, True, False, False, False])

    def test_static_prefill_masks_columns_and_pads_missing_prefix_lengths(self):
        """Static [2, 3] rows contain just two real columns for request 3."""
        req, pos, valid = derive_kv_rows(
            6,
            is_decode=False,
            max_context_len=16,
            req_pool_indices=torch.tensor([3, 0]),
            extend_seq_lens=torch.tensor([2, 0]),
            extend_prefix_lens=torch.tensor([6]),
            extend_seq_lens_cpu=[2, 0],
        )
        self.assertEqual(req.tolist(), [3, 3, 3, 0, 0, 0])
        self.assertEqual(pos.tolist(), [6, 7, 8, 0, 1, 2])
        self.assertEqual(valid.tolist(), [True, True, False, False, False, False])

    def test_empty_batch_keeps_static_rows_invalid(self):
        """An empty logical batch can retain a padded tensor extent."""
        req, pos, valid = derive_kv_rows(
            16,
            is_decode=False,
            max_context_len=16,
            req_pool_indices=torch.empty(0, dtype=torch.long),
            extend_seq_lens=torch.empty(0, dtype=torch.long),
            extend_prefix_lens=torch.empty(0, dtype=torch.long),
        )
        self.assertEqual(req.tolist(), [-1] * 16)
        self.assertFalse(valid.any().item())

    def test_invalid_extents_and_missing_layout_fields_fail(self):
        """Invalid source rows cannot be mistaken for another layout."""
        common = dict(
            is_decode=False,
            max_context_len=16,
            req_pool_indices=torch.tensor([1, 2]),
            extend_seq_lens=torch.tensor([2, 1]),
            extend_prefix_lens=torch.tensor([0, 0]),
            extend_seq_lens_cpu=[2, 1],
        )
        with self.assertRaisesRegex(ValueError, "row-major"):
            derive_kv_rows(5, **common)
        with self.assertRaisesRegex(ValueError, "requires"):
            derive_kv_rows(
                2,
                is_decode=True,
                max_context_len=16,
                req_pool_indices=torch.tensor([1, 2]),
            )
        common["extend_prefix_lens"] = torch.tensor([0, 0, 0])
        with self.assertRaisesRegex(ValueError, "prefix"):
            derive_kv_rows(3, **common)


if __name__ == "__main__":
    unittest.main()
