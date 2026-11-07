"""Cold materialization descriptors use device ops, without host tensor uploads."""

import unittest
from unittest.mock import patch

import torch
from test_multibase import cpu_multibase

from ascend_mempool.multibase import BaseTable, MultibaseCopy


class TestCaptureInitialization(unittest.TestCase):
    def test_first_call_and_shape_change_require_no_host_tensor_upload(self):
        for rows in (2, 3):
            sources = [torch.arange(rows, dtype=torch.int64) + 10 * i for i in range(4)]
            output = torch.full((rows,), -1, dtype=torch.int64)
            indices = torch.arange(rows)
            mask = torch.ones(rows, dtype=torch.bool)
            with patch.object(
                torch, "tensor", side_effect=AssertionError("host tensor upload")
            ):
                source = BaseTable(
                    [(t.data_ptr(), rows) for t in sources], "cpu", sources
                )
                target = BaseTable([(output.data_ptr(), rows)], "cpu", [output])
                copier = MultibaseCopy(
                    source, target, rows, row_bytes=8, kernel=cpu_multibase
                )
                addresses = [t.data_ptr() for t in copier.metadata]
                for region in range(4):
                    copier.submit(indices + region * rows, indices, mask)
                    torch.testing.assert_close(output, sources[region], rtol=0, atol=0)
                    self.assertEqual(addresses, [t.data_ptr() for t in copier.metadata])
