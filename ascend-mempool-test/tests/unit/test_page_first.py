"""Independent storage-order and wide-address checks for page-first KV."""

import unittest

import torch
from multibase_oracle import MappedKernel
from test_pool import FakeBM

from ascend_mempool.copy import SparseCopyInputs, SparseKVCopy
from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.offload import MempoolKVOffload


class TestPageFirstLayout(unittest.TestCase):
    def test_address_above_four_tb_uses_one_kernel_submission(self):
        layout = KVLayout(78, 16, 1 << 24, 1, 576, page_size=128)
        manager = MempoolKVManager.create(
            PoolLayout(layout, layout), 0, bm_module=FakeBM(0)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        calls = []

        def kernel(src, dst, si, di, mask, sr, dr, rb, count, block, sp, dp):
            calls.append(dp + int(di[0]) * rb)

        MempoolKVOffload(manager.view(0, 77), kernel=kernel).write(
            torch.ones((1, 1, 576), dtype=torch.bfloat16),
            slots=torch.tensor([15]),
            positions=torch.tensor([(1 << 24) - 1]),
            valid=torch.tensor([True]),
        )
        # Last physical row of a row-major [page,layer,token,feature] arena.
        self.assertEqual(calls, [0x5000000000 + 78 * 16 * (1 << 24) * 1152 - 1152])
        self.assertGreater(calls[0] - 0x5000000000, 4 * (1 << 40))

    def test_four_decode_geometries_match_physical_tensor_oracle(self):
        for b, sd in ((128, 128), (128, 512), (64, 128), (64, 512)):
            layout = KVLayout(3, 2, sd, 1, 4, page_size=b)
            # Independent row-major tensor enumeration is the address oracle.
            physical = torch.arange(layout.total_bytes // 2).reshape(
                2, sd // b, 3, b, 1, 4
            )
            for slot in (0, 1):
                for page in range(sd // b):
                    for token in (0, 1, b - 1):
                        for layer in range(3):
                            self.assertEqual(
                                layout.byte_offset(layer, slot, page * b + token),
                                int(physical[slot, page, layer, token, 0, 0]) * 2,
                            )

    def test_physical_order_and_page_blocks_match_independent_enumeration(self):
        layout = KVLayout(3, 2, 4, 2, 3, page_size=2)
        # Enumerate storage order, rather than repeating byte_offset's formula.
        coordinates = [
            (layer, slot, page * 2 + token, head, feature)
            for slot in range(2)
            for page in range(2)
            for layer in range(3)
            for token in range(2)
            for head in range(2)
            for feature in range(3)
        ]
        for element, coordinate in enumerate(coordinates):
            self.assertEqual(layout.byte_offset(*coordinate), element * 2)
        self.assertEqual(layout.page_bytes, 72)
        self.assertEqual(layout.page_offset(3), 216)
        self.assertEqual(layout.total_bytes, 288)

    def test_real_prompt_geometry_keeps_last_slot_addresses_above_uint32(self):
        layout = KVLayout(78, 16, 8192, 1, 576, page_size=128)
        self.assertEqual(layout.page_bytes, 11501568)
        self.assertEqual(layout.slot_bytes, 736100352)
        self.assertEqual(layout.page_offset(960), 11041505280)
        self.assertEqual(layout.byte_offset(77, 15, 8191, 0, 575), 11777605630)
        self.assertEqual(layout.storage_order, "page_first")

    def test_large_component_uses_one_64_bit_base(self):
        geometry = KVLayout(78, 16, 65536, 1, 576, page_size=128)
        manager = MempoolKVManager.create(
            PoolLayout(geometry, geometry), 0, bm_module=FakeBM(0)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        addresses = []

        def kernel(src, dst, si, di, mask, sr, dr, rb, count, block, sp, dp):
            self.assertGreater(dr * rb, 2**32 - 1)
            addresses.extend(dp + int(index) * rb for index in di[mask])

        MempoolKVOffload(manager.view(0, 77), kernel=kernel).write(
            torch.ones((1, 1, 576), dtype=torch.bfloat16),
            slots=torch.tensor([15]),
            positions=torch.tensor([65535]),
            valid=torch.tensor([True]),
        )
        self.assertEqual(addresses, [0x5000000000 + 94220843904])

    def test_write_and_fetch_use_real_physical_storage_with_feature_parts(self):
        geometry = KVLayout(3, 3, 4, 2, 4, page_size=2)
        manager = MempoolKVManager.create(
            PoolLayout(geometry, geometry), 1, bm_module=FakeBM(1)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        buffers = {
            side: torch.full((3, 2, 3, 2, 2, 4), -17, dtype=torch.bfloat16)
            for side in (0, 1)
        }
        bases = {manager.view(side, 0).device_base: buffers[side] for side in (0, 1)}

        def kernel(src, dst, si, di, valid, sr, dr, rb, count, block, sp, dp):
            def resolve(tensor, pointer, rows):
                if pointer is None:
                    return tensor.reshape(rows, rb // 2)
                base = max(base for base in bases if base <= pointer)
                offset = (pointer - base) // 2
                return (
                    bases[base]
                    .flatten()[offset : offset + rows * rb // 2]
                    .view(rows, rb // 2)
                )

            resolve(dst, dp, dr)[di[valid]] = resolve(src, sp, sr)[si[valid]]

        values = torch.arange(16, dtype=torch.bfloat16).reshape(2, 2, 4)
        slots, positions = torch.tensor([0, 2]), torch.tensor([1, 2])
        mask = torch.tensor([True, True])
        for part in (0, 1):
            MempoolKVOffload(
                manager.view(1, 1), kernel=kernel, feature_part=(part, 2)
            ).write(values, slots=slots, positions=positions, valid=mask)
        expected = torch.full_like(buffers[1], -17)
        expected[0, 0, 1, 1] = values[0]
        expected[2, 1, 1, 0] = values[1]
        torch.testing.assert_close(buffers[1], expected, rtol=0, atol=0)
        # Read values labelled directly in the physical tensor, independently of writes.
        buffers[0][1, 1, 1, 1] = 31
        inputs = SparseCopyInputs(2, 2, "cpu")
        inputs.active.fill_(True)
        inputs.p_slots.fill_(1)
        inputs.d_slots.copy_(slots)
        inputs.prompt_lengths.fill_(4)
        inputs.decode_lengths.fill_(3)
        inputs.positions.copy_(torch.tensor([[3, 5], [6, -1]]))
        inputs.valid.fill_(True)
        gather = SparseKVCopy(
            manager.view(0, 1), manager.view(1, 1), inputs, kernel=MappedKernel(bases)
        )
        gather.output.fill_(-9)
        result = gather.gather()
        torch.testing.assert_close(result[0, 0], torch.full_like(values[0], 31))
        torch.testing.assert_close(result[0, 1], values[0])
        torch.testing.assert_close(result[1, 0], values[1])
        self.assertTrue((result[1, 1] == -9).all())
        first = manager.view(1, 2).page_block(0)
        second = manager.view(1, 0).page_block(1)
        self.assertEqual(first[2], 96)
        self.assertEqual(second[0] - first[0], first[2])
        self.assertEqual(second[1] - first[1], first[2])

    def test_last_slot_write_uses_64_bit_offsets_and_updates_device_bindings(
        self,
    ):
        geometry = KVLayout(78, 16, 8192, 1, 576, page_size=128)
        manager = MempoolKVManager.create(
            PoolLayout(geometry, geometry), 0, bm_module=FakeBM(0)
        )
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        addresses = []

        def kernel(src, dst, si, di, valid, sr, dr, rb, count, block, sp, dp):
            self.assertGreater(max(sr, dr) * rb, 2**32 - 1)
            self.assertLessEqual(rb, 32768)
            addresses.extend(dp + int(index) * rb for index in di[valid])

        writer = MempoolKVOffload(manager.view(0, 77), kernel=kernel)
        slots, positions, valid = (
            torch.tensor([15]),
            torch.tensor([8191]),
            torch.tensor([False]),
        )
        pointers = [t.data_ptr() for t in (slots, positions, valid)]
        values = torch.ones((1, 1, 576), dtype=torch.bfloat16)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        self.assertEqual(addresses, [])
        valid.fill_(True)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        self.assertEqual(addresses, [0x5000000000 + 11777604480])
        slots.fill_(0)
        writer.write(values, slots=slots, positions=positions, valid=valid)
        self.assertEqual(addresses[-1], 0x5000000000 + 736099200)
        self.assertEqual(pointers, [t.data_ptr() for t in (slots, positions, valid)])


if __name__ == "__main__":
    unittest.main()
