import unittest

from ascend_mempool.layout import KVLayout, PoolLayout


class TestKVLayout(unittest.TestCase):
    def test_default_mla_layout_and_logical_element(self):
        layout = KVLayout(layers=2, slots=16, tokens=16384, heads=1, dim=576)
        self.assertEqual(layout.shape, (16, 16384, 1, 576))
        self.assertEqual(layout.dtype, "bfloat16")
        self.assertEqual(layout.row_bytes, 1152)
        self.assertEqual(layout.layer_bytes, 301989888)
        self.assertEqual(layout.total_bytes, 603979776)
        self.assertEqual(layout.row_index(slot=2, token=3), 32771)
        self.assertEqual(
            layout.byte_offset(layer=1, slot=0, token=1, head=0, column=7),
            301991054,
        )

    def test_out_of_bounds_coordinates_cannot_produce_addresses(self):
        layout = KVLayout(layers=2, slots=16, tokens=16384, heads=1, dim=576)
        for coordinate in (
            {"layer": -1},
            {"layer": 2},
            {"slot": -1},
            {"slot": 16},
            {"token": -1},
            {"token": 16384},
            {"head": 1},
            {"column": 576},
        ):
            with self.subTest(coordinate=coordinate):
                values = dict(layer=0, slot=0, token=0, head=0, column=0)
                values.update(coordinate)
                with self.assertRaises(IndexError):
                    layout.byte_offset(**values)

    def test_unsupported_copy_layouts_fail_before_allocation(self):
        for override in (
            {"layers": 0},
            {"slots": -1},
            {"tokens": 0},
            {"heads": 1.5},
            {"dim": True},
            {"dtype": "float32"},
            {"dim": 16385},
            {"tokens": 1 << 31},
        ):
            with self.subTest(override=override):
                values = dict(layers=1, slots=1, tokens=1, heads=1, dim=1)
                values.update(override)
                with self.assertRaises(ValueError):
                    KVLayout(**values)
        self.assertEqual(
            KVLayout(layers=20, slots=16, tokens=16384, heads=1, dim=576).total_bytes,
            6039797760,
        )
        self.assertEqual(
            KVLayout(layers=2, slots=1, tokens=2147483647, heads=1, dim=1).layer_bytes,
            4294967294,
        )

    def test_asymmetric_contributions_share_one_rank_stride(self):
        prompt = KVLayout(layers=2, slots=16, tokens=16384, heads=1, dim=576)
        decode = KVLayout(layers=2, slots=16, tokens=32768, heads=1, dim=576)
        pool = PoolLayout(prompt, decode)
        self.assertEqual(pool.contribution_bytes(0), 1073741824)
        self.assertEqual(pool.contribution_bytes(1), 2147483648)
        self.assertEqual(pool.rank_stride_bytes, 2147483648)
        self.assertEqual(pool.probe_offset(0), 603979776)
        self.assertEqual(pool.probe_offset(1), 1207959552)
        self.assertEqual(pool.layout_for_rank(0), prompt)
        self.assertEqual(pool.layout_for_rank(1), decode)

    def test_incompatible_peers_and_alignment_are_rejected(self):
        prompt = KVLayout(layers=2, slots=16, tokens=16, heads=1, dim=576)
        for override in ({"layers": 1}, {"slots": 8}, {"heads": 2}, {"dim": 128}):
            with self.subTest(override=override):
                values = dict(layers=2, slots=16, tokens=32, heads=1, dim=576)
                values.update(override)
                with self.assertRaises(ValueError):
                    PoolLayout(prompt, KVLayout(**values))
        for alignment in (0, -1, 3):
            with self.subTest(alignment=alignment):
                with self.assertRaises(ValueError):
                    PoolLayout(prompt, prompt, alignment_bytes=alignment)


if __name__ == "__main__":
    unittest.main()
