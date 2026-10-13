"""Execute the C/D pointer copies against host-backed shared and private pools."""

import unittest
from types import SimpleNamespace

import torch

from ascend_mempool.cd_copy import CDWorkload, write_values
from ascend_mempool.cd_plan import CDCase
from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.verification import kv_pattern


class Arena:
    def __init__(self, n, mode, side, tokens=128):
        self.memory = {}
        self.pools = []
        self.layout = PoolLayout(
            KVLayout(1, 16, tokens, 1, 16), KVLayout(1, 16, tokens, 1, 16)
        )
        for member in range(n):
            views = {}
            for source in (0, 1):
                pointer = 1000 + source * 100 + (0 if mode == "shared" else member)
                if pointer not in self.memory:
                    self.memory[pointer] = torch.stack(
                        [
                            kv_pattern(source, 0, slot, 0, tokens, 1, 16)
                            for slot in range(16)
                        ]
                    )

                def write_rows(slot, token, values, pointer=pointer):
                    self.memory[pointer][slot, token : token + len(values)].copy_(
                        values
                    )

                views[source] = SimpleNamespace(
                    device_base=pointer, write_rows=write_rows
                )
            self.pools.append(
                SimpleNamespace(
                    layout=self.layout,
                    tp_size=n,
                    mode=mode,
                    member=member,
                    rank=side,
                    view=lambda source, layer, views=views: views[source],
                )
            )

    def kernel(self, src, dst, si, di, valid, sr, dr, size, count, block, sp, dp):
        assert block == 48
        assert len(si) == len(di) == len(valid) == count
        source = (self.memory[sp] if sp is not None else src).reshape(sr, size // 2)
        target = (self.memory[dp] if dp is not None else dst).reshape(dr, size // 2)
        target[di[valid]] = source[si[valid]]

    def workloads(self, case):
        return [
            CDWorkload(p, case, seed=9, device="cpu", kernel=self.kernel, topk=8)
            for p in self.pools
        ]


class TestCDCopy(unittest.TestCase):
    def test_write_generations_preserve_every_byte_marker_in_bf16(self):
        source = torch.arange(256, dtype=torch.int16).to(torch.bfloat16)
        for generation in (0, 1, 21, 41, 61, 255):
            encoded = write_values(source, generation)
            self.assertEqual(encoded.dtype, torch.bfloat16)
            self.assertEqual(encoded.unique().numel(), 256)
            self.assertTrue((encoded < 0).all())
        self.assertFalse(torch.equal(write_values(source, 0), write_values(source, 1)))

    def test_shared_disjoint_bulk_writes_union_and_guard_checks(self):
        for n in (1, 2, 4):
            for side in (0, 1):
                arena = Arena(n, "shared", side, tokens=4096)
                works = arena.workloads(CDCase("tail", "logical", rows=2049))
                for variant in (0, 1):
                    for w in works:
                        w.prepare(variant)
                        w.reset()
                    for w in works:
                        w.launch()
                    for w in works:
                        w.verify()
                address = works[0].pool.view(side, 0).device_base
                arena.memory[address][0, 2050, 0, 3] = 199
                with self.assertRaises(AssertionError):
                    works[0].verify()

    def test_missing_shared_writer_is_detected(self):
        arena = Arena(4, "shared", 0)
        works = arena.workloads(CDCase("write", "logical", rows=64))
        for w in works[:-1]:
            w.launch()
        with self.assertRaises(AssertionError):
            works[0].verify()

    def test_independent_control_keeps_other_spans_and_full_mode_writes_all(self):
        for mode in ("logical", "equal_total"):
            arena = Arena(4, "independent", 1)
            works = arena.workloads(CDCase("write", mode, rows=65))
            for w in works:
                w.prepare(3)
                w.launch()
                w.verify()
                w.reset()
            for member in range(4):
                original = torch.stack(
                    [kv_pattern(1, 0, slot, 0, 128, 1, 16) for slot in range(16)]
                )
                self.assertTrue(torch.equal(original, arena.memory[1100 + member]))

    def test_all_readers_get_full_selection_with_stable_graph_addresses(self):
        for mode in ("independent", "shared"):
            for side in (0, 1):
                for source in ("local", "remote", "mixed"):
                    arena = Arena(4, mode, side)
                    works = arena.workloads(
                        CDCase("read", "read", active=16, source=source)
                    )
                    pointers = [
                        [t.data_ptr() for route in w.routes for t in route]
                        for w in works
                    ]
                    outputs = []
                    for variant in (0, 1):
                        for m, w in enumerate(works):
                            w.prepare(variant)
                            w.launch()
                            w.verify()
                            self.assertEqual(w.payload, 16 * 8 * 32)
                            self.assertEqual(
                                pointers[m],
                                [t.data_ptr() for route in w.routes for t in route],
                            )
                            self.assertTrue(torch.equal(works[0].tokens, w.tokens))
                        self.assertTrue(
                            all(torch.equal(works[0].output, w.output) for w in works)
                        )
                        outputs.append(works[0].output.clone())
                    self.assertFalse(torch.equal(*outputs))
                    if source == "mixed":
                        self.assertEqual(int((works[0].sources == 0).sum()), 16 * 4)

    def test_wrong_remote_source_or_wrong_output_order_is_detected(self):
        arena = Arena(2, "shared", 0)
        w = arena.workloads(CDCase("read", "read", source="remote"))[0]
        w.launch()
        w.verify()
        w.output.copy_(w.output.flip(0))
        with self.assertRaises(AssertionError):
            w.verify()
        arena.memory[1100].copy_(arena.memory[1000])
        w.launch()
        with self.assertRaises(AssertionError):
            w.verify()


if __name__ == "__main__":
    unittest.main()
