"""Independent physical tensor oracle for request page-table writes and reads."""

import unittest

import torch
from multibase_oracle import MappedKernel
from test_pool import FakeBM

from ascend_mempool.alive_pages import PageLease
from ascend_mempool.copy import SparseCopyInputs, SparseKVCopy
from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.offload import MempoolKVOffload


class TestDeviceAlivePages(unittest.TestCase):
    def test_scattered_read_write_rebind_and_zero_rows(self):
        layout = KVLayout(2, 3, 4, 1, 2, page_size=2)
        manager = MempoolKVManager.create(
            PoolLayout(layout, layout), 1, bm_module=FakeBM(1)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        manager.enable_page_tables("cpu")
        manager.install_pages(0, 0, PageLease("r", 1, (3, 0)))
        manager.install_pages(1, 0, PageLease("r", 1, (4, 1, 5)))
        table = manager.page_tables[1, "alive_main"]
        table_ptr = table.data_ptr()
        memory = {
            side: torch.full((6, 2, 2, 1, 2), -9, dtype=torch.bfloat16)
            for side in (0, 1)
        }
        bases = {manager.view(side, 0).device_base: memory[side] for side in (0, 1)}

        def kernel(src, dst, si, di, mask, sr, dr, rb, count, block, sp, dp):
            def resolve(tensor, pointer, rows):
                if pointer is None:
                    return tensor.reshape(rows, rb // 2)
                base = max(value for value in bases if value <= pointer)
                offset = (pointer - base) // 2
                return (
                    bases[base]
                    .flatten()[offset : offset + rows * rb // 2]
                    .view(rows, rb // 2)
                )

            resolve(dst, dp, dr)[di[mask]] = resolve(src, sp, sr)[si[mask]]

        writer = MempoolKVOffload(manager.view(1, 1), kernel=kernel)
        writer.write(
            torch.tensor([[[21, 22]], [[31, 32]]], dtype=torch.bfloat16),
            slots=torch.tensor([0, 0]),
            positions=torch.tensor([0, 5]),
            valid=torch.tensor([True, True]),
        )
        expected = torch.full_like(memory[1], -9)
        expected[4, 1, 0, 0] = torch.tensor([21, 22])
        expected[5, 1, 1, 0] = torch.tensor([31, 32])
        torch.testing.assert_close(memory[1], expected, rtol=0, atol=0)
        # Label physical storage independently of the production writer.
        memory[0][3, 1, 1] = 101
        memory[0][0, 1, 0] = 102
        memory[1][1, 1, 1] = 203
        inputs = SparseCopyInputs(1, 3, "cpu")
        inputs.active.fill_(True)
        inputs.p_slots.fill_(0)
        inputs.d_slots.fill_(0)
        inputs.prompt_lengths.fill_(4)
        inputs.decode_lengths.fill_(6)
        inputs.positions.copy_(torch.tensor([[1, 2, 7]]))
        inputs.valid.fill_(True)
        reader = SparseKVCopy(
            manager.view(0, 1), manager.view(1, 1), inputs, kernel=MappedKernel(bases)
        )
        self.assertEqual(
            reader.gather().flatten().tolist(), [101, 101, 102, 102, 203, 203]
        )
        manager.clear_pages(1, 0, PageLease("r", 1, (4, 1, 5)))
        manager.install_pages(1, 0, PageLease("next", 2, (2, 5, 0)))
        self.assertEqual(table.data_ptr(), table_ptr)
        memory[1][5, 1, 1] = 304
        self.assertEqual(
            reader.gather().flatten().tolist(), [101, 101, 102, 102, 304, 304]
        )
        with self.assertRaisesRegex(RuntimeError, "stale"):
            manager.install_pages(1, 0, PageLease("r", 1, (4, 1, 5)))
        inputs.active.zero_()
        reader.output.fill_(-7)
        self.assertEqual(reader.gather().flatten().tolist(), [-7] * 6)
