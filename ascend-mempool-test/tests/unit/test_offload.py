"""Verify temporary KV writes through the external UniDexCopy boundary."""

import unittest

import torch
from test_pool import FakeBM

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.offload import MempoolKVOffload
from ascend_mempool.pool import MempoolKVManager


class CPUWriteKernel:
    """Emulate the external raw-destination kernel with real CPU tensor contents."""

    def __init__(self, destination, pointer):
        """Associate a process-local address with its simulated DRAM tensor."""
        self.destination = destination
        self.pointer = pointer

    def __call__(
        self,
        src,
        dst,
        src_index,
        dst_index,
        valid,
        src_rows,
        dst_rows,
        row_bytes,
        max_copy,
        block_dim,
        src_ptr,
        dst_ptr,
    ):
        """Apply valid indexed writes, checking the declared storage extents."""
        if src_ptr is not None or dst_ptr != self.pointer:
            raise ValueError("unexpected copy address")
        if src_rows * row_bytes != src.numel() * src.element_size():
            raise ValueError("source extent mismatch")
        if dst_rows * row_bytes != self.destination.numel() * 2:
            raise ValueError("destination extent mismatch")
        source = src.reshape(src_rows, row_bytes // 2)
        target = self.destination.reshape(dst_rows, row_bytes // 2)
        target[dst_index[valid]] = source[src_index[valid]]


class TestMempoolKVOffload(unittest.TestCase):
    """Check logical writes without mocking the mempool manager or index logic."""

    def setUp(self):
        """Open a small decode pool and a sentinel-filled simulated DRAM layer."""
        layer = KVLayout(layers=1, slots=16, tokens=8, heads=1, dim=4)
        self.manager = MempoolKVManager.create(
            PoolLayout(layer, layer), rank=1, bm_module=FakeBM(1)
        )
        self.manager.join(timeout=0.1)
        self.view = self.manager.view(1, 0)
        self.target = torch.full(self.view.shape, -1, dtype=torch.bfloat16)
        self.kernel = CPUWriteKernel(self.target, self.view.device_base)

    def tearDown(self):
        """Drain the fake device before releasing its pool."""
        self.manager.close(drain=lambda: None)

    def test_writes_logical_rows_and_leaves_padding_and_invalid_coordinates_untouched(
        self,
    ):
        """Write first/last tokens while masking unbound and out-of-range rows."""
        slots = torch.tensor([2, 15, 16, -1, 1, 1, 1, 3])
        positions = torch.tensor([0, 7, 0, 0, 8, -1, 3, 2])
        valid = torch.tensor([True, True, True, True, True, True, False, True])
        values = torch.tensor(
            [
                [11, 12, 13, 14],
                [21, 22, 23, 24],
                [31, 32, 33, 34],
                [41, 42, 43, 44],
                [51, 52, 53, 54],
                [61, 62, 63, 64],
                [71, 72, 73, 74],
                [81, 82, 83, 84],
            ],
            dtype=torch.bfloat16,
        ).reshape(8, 1, 4)
        writer = MempoolKVOffload(self.view, kernel=self.kernel)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        expected = torch.full_like(self.target, -1)
        expected[2, 0, 0] = torch.tensor([11, 12, 13, 14])
        expected[15, 7, 0] = torch.tensor([21, 22, 23, 24])
        expected[3, 2, 0] = torch.tensor([81, 82, 83, 84])
        self.assertTrue(torch.equal(self.target, expected))

    def test_unbound_warmup_and_changed_bindings_use_the_same_input_buffers(self):
        """Enable writes after zero-valid warmup, then reuse the writer for new slots."""
        slots = torch.full((2,), -1, dtype=torch.int64)
        positions = torch.full((2,), -1, dtype=torch.int64)
        valid = torch.zeros(2, dtype=torch.bool)
        addresses = [tensor.data_ptr() for tensor in (slots, positions, valid)]
        writer = MempoolKVOffload(self.view, kernel=self.kernel)
        values = torch.tensor(
            [[11, 12, 13, 14], [21, 22, 23, 24]], dtype=torch.bfloat16
        ).reshape(2, 1, 4)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        self.assertTrue((self.target == -1).all().item())

        slots.copy_(torch.tensor([4, 6]))
        positions.copy_(torch.tensor([1, 2]))
        valid.fill_(True)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        expected = torch.full_like(self.target, -1)
        expected[4, 1, 0] = torch.tensor([11, 12, 13, 14])
        expected[6, 2, 0] = torch.tensor([21, 22, 23, 24])
        self.assertTrue(torch.equal(self.target, expected))

        slots.copy_(torch.tensor([8, 9]))
        positions.copy_(torch.tensor([0, 7]))
        valid.copy_(torch.tensor([True, False]))
        values.fill_(99)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        expected[8, 0, 0] = 99
        self.assertTrue(torch.equal(self.target, expected))
        self.assertEqual(
            [tensor.data_ptr() for tensor in (slots, positions, valid)],
            addresses,
        )

    def test_rejects_wrong_source_schema_remote_writes_and_closed_pools(self):
        """Reject static contract errors before the copy can access DRAM."""
        slots = torch.full((2,), -1, dtype=torch.int64)
        positions = torch.full((2,), -1, dtype=torch.int64)
        valid = torch.zeros(2, dtype=torch.bool)
        writer = MempoolKVOffload(self.view, kernel=self.kernel)
        values = torch.ones((2, 1, 4), dtype=torch.bfloat16)
        invalid_sources = (values.float(), values[:1], values.transpose(0, 2))
        for source in invalid_sources:
            with self.subTest(shape=source.shape, dtype=source.dtype):
                with self.assertRaises(ValueError):
                    writer.write(source, slots=slots, positions=positions, valid=valid)
        noncontiguous = torch.ones((2, 1, 8), dtype=torch.bfloat16)[:, :, ::2]
        with self.assertRaisesRegex(ValueError, "contiguous"):
            writer.write(noncontiguous, slots=slots, positions=positions, valid=valid)
        with self.assertRaisesRegex(ValueError, "NPU source"):
            MempoolKVOffload(self.view).write(
                values, slots=slots, positions=positions, valid=valid
            )
        with self.assertRaisesRegex(ValueError, "owning rank"):
            MempoolKVOffload(self.manager.view(0, 0), kernel=self.kernel)
        self.manager.close(drain=lambda: None)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            writer.write(values, slots=slots, positions=positions, valid=valid)
        self.assertTrue((self.target == -1).all().item())

    def test_explicit_metadata_supports_variable_eager_chunk_sizes(self):
        """One writer handles a short chunk and then a larger chunk."""
        writer = MempoolKVOffload(self.view, kernel=self.kernel)
        writer.write(
            torch.full((1, 1, 4), 11, dtype=torch.bfloat16),
            slots=torch.tensor([2]),
            positions=torch.tensor([0]),
            valid=torch.tensor([True]),
        )
        writer.write(
            torch.full((3, 1, 4), 22, dtype=torch.bfloat16),
            slots=torch.tensor([2, 2, -1]),
            positions=torch.tensor([1, 2, 0]),
            valid=torch.tensor([True, True, False]),
        )
        expected = torch.full_like(self.target, -1)
        expected[2, 0] = 11
        expected[2, 1:3] = 22
        self.assertTrue(torch.equal(self.target, expected))

    def test_zero_valid_still_reaches_kernel_and_partial_metadata_is_rejected(self):
        """Capture includes the write call even when its mask selects no rows."""
        calls = []

        def kernel(*args):
            """Observe whether the external copy boundary was submitted."""
            calls.append(args[6])

        writer = MempoolKVOffload(self.view, kernel=kernel)
        values = torch.zeros((3, 1, 4), dtype=torch.bfloat16)
        writer.write(
            values,
            slots=torch.full((3,), -1),
            positions=torch.zeros(3, dtype=torch.long),
            valid=torch.zeros(3, dtype=torch.bool),
        )
        self.assertEqual(calls, [128])
        with self.assertRaises(TypeError):
            writer.write(values, slots=torch.tensor([1, 1, 1]))


if __name__ == "__main__":
    unittest.main()
