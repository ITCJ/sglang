import unittest

import torch

from ascend_mempool.verification import kv_pattern


class TestVerificationPayload(unittest.TestCase):
    def test_bf16_pattern_identifies_source_layer_slot_and_token(self):
        pattern = kv_pattern(
            rank=1, layer=2, slot=3, first_token=258, count=1, heads=1, dim=8
        )
        self.assertEqual(pattern.dtype, torch.bfloat16)
        self.assertEqual(pattern.shape, (1, 1, 8))
        self.assertEqual(pattern.flatten().tolist(), [2, 2, 3, 2, 1, 0, 6, 43])


if __name__ == "__main__":
    unittest.main()
