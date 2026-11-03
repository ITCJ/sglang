import unittest

from run_k1_full_acceptance import configurations

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestK1Matrix(unittest.TestCase):
    def test_matrix_preserves_prefix_tokens_and_sets_every_final_feature(self):
        rows = configurations(RouterConfig("p", "d", "/model"), "patched-sdk")
        self.assertEqual(
            [(c.page_size, c.decode_capacity, c.prefix_pages) for _, c in rows],
            [(128, 128, 40), (128, 512, 40), (64, 128, 80), (64, 512, 80)],
        )
        for _, c in rows:
            self.assertTrue(c.pool_copy and c.prefix_shared)
            self.assertTrue(
                c.prefill_shared and c.decode_shared and c.prefill_index_shared
            )
            self.assertEqual(c.read_assignment, "by_segment")
            self.assertGreaterEqual(
                c.context_length, c.prompt_capacity + c.decode_capacity + 2
            )
