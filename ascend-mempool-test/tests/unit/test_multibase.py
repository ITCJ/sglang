"""Exercise fixed multibase metadata through the external kernel boundary."""

import ctypes
import unittest

import torch

from ascend_mempool.multibase import BaseTable, MultibaseCopy


def cpu_multibase(sb, sr, sv, db, dr, dv, si, di, mask, width, blocks):
    """Independent linear address oracle; no production routing helpers."""
    for source, target, valid in zip(si.tolist(), di.tolist(), mask.tolist()):
        if not valid:
            continue
        src = [
            int(b) + (source - int(r[0])) * width
            for b, r, v in zip(sb, sr, sv)
            if v and int(r[0]) <= source < int(r[1])
        ]
        dst = [
            int(b) + (target - int(r[0])) * width
            for b, r, v in zip(db, dr, dv)
            if v and int(r[0]) <= target < int(r[1])
        ]
        if src and dst:
            ctypes.memmove(dst[0], src[0], width)


class TestMultibase(unittest.TestCase):
    def test_one_hbm_row_can_write_two_targets_with_independent_masks(self):
        source = torch.tensor([17, 23], dtype=torch.int32)
        left = torch.full((2,), -1, dtype=torch.int32)
        right = torch.full((2,), -1, dtype=torch.int32)
        copier = MultibaseCopy(
            BaseTable([(source.data_ptr(), 2)], "cpu", [source]),
            BaseTable(
                [(left.data_ptr(), 2), (right.data_ptr(), 2)], "cpu", [left, right]
            ),
            4,
            row_bytes=4,
            kernel=cpu_multibase,
        )
        copier.submit(
            torch.tensor([0, 1, 0, 1]),
            torch.tensor([0, 1, 2, 3]),
            torch.tensor([True, False, True, True]),
        )
        self.assertEqual(left.tolist(), [17, -1])
        self.assertEqual(right.tolist(), [17, 23])

    def test_reserved_location_switch_and_invalidation_keep_fixed_storage(self):
        from ascend_mempool.multibase_fixture import PageLocations

        table = PageLocations(1, 2, 0, "cpu")
        table.install(0, torch.tensor([3, 1]))
        table.regions[0, 0, 1] = 2
        table.pages[0, 0, 1] = 7
        table.valid[0, 0, 1] = True
        pointers = [
            x.data_ptr()
            for x in (table.regions, table.pages, table.valid, table.selected)
        ]
        handle, logical = torch.tensor([0]), torch.tensor([0])
        table.selected[0, 0] = 1
        self.assertEqual(
            [x.tolist() for x in table.resolve(handle, logical)], [[2], [7], [True]]
        )
        table.valid[0, 0, 1] = False
        self.assertEqual(
            [x.tolist() for x in table.resolve(handle, logical)], [[0], [3], [True]]
        )
        self.assertEqual(
            pointers,
            [
                x.data_ptr()
                for x in (table.regions, table.pages, table.valid, table.selected)
            ],
        )
        table.install(0, torch.tensor([4, 2]))
        self.assertEqual(
            [x.tolist() for x in table.resolve(handle, logical)], [[0], [4], [True]]
        )

    def test_fixed_tables_route_twelve_sources_and_mask_reserved_targets(self):
        sources = [
            torch.tensor([100 + i, 200 + i], dtype=torch.int32) for i in range(12)
        ]
        output = torch.full((13,), -1, dtype=torch.int32)
        src = BaseTable([(t.data_ptr(), 2) for t in sources], "cpu", sources)
        dst = BaseTable([(output.data_ptr(), 13)], "cpu", [output])
        copy = MultibaseCopy(src, dst, 13, row_bytes=4, kernel=cpu_multibase)
        pointers = [t.data_ptr() for t in copy.metadata]
        si = torch.tensor([2 * i + 1 for i in range(12)] + [0])
        di = torch.arange(13)
        mask = torch.tensor([True] * 12 + [False])
        copy.submit(si, di, mask)
        self.assertEqual(output.tolist(), list(range(200, 212)) + [-1])
        src.valid[5] = False
        output.fill_(-1)
        copy.submit(si, di, mask)
        self.assertEqual(
            output.tolist(), list(range(200, 205)) + [-1] + list(range(206, 212)) + [-1]
        )
        self.assertEqual(pointers, [t.data_ptr() for t in copy.metadata])


if __name__ == "__main__":
    unittest.main()
