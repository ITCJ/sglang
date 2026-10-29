"""Restore real mapped prefix pages through a CPU stand-in for HCCL."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import test_prefix
import torch
from test_mempool_native_prefix import native_methods
from test_pd_tick import CPUCollective


class TensorCollective:
    def __init__(self, size):
        self.collective = CPUCollective(size)

    def rank(self, rank):
        def gather(output, value):
            assert (
                output.untyped_storage().data_ptr()
                == value.untyped_storage().data_ptr()
            )
            assert (
                value.data_ptr()
                == output.data_ptr() + rank * value.numel() * value.element_size()
            )
            parts = self.collective.gather(rank, value.clone())
            output.copy_(torch.cat(parts))

        return gather


class TestSplitRead(unittest.TestCase):
    def test_gathered_contents_must_match_the_private_replica(self):
        helper = test_prefix.TestPrefixStore()
        self.addCleanup(helper.doCleanups)

        def corrupt_gather(output, value):
            output.copy_(torch.cat((value, value)))
            output[0] += 1

        store, manager, kernel, events = helper.make_store(
            verify=True, readers=2, all_gather=corrupt_gather
        )
        store.begin_request("A", 3, 2)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                alive = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                values = torch.full_like(alive[3, :2], 17 + layer)
                alive[3, :2] = values
                store.capture_source(
                    region,
                    layer,
                    values,
                    torch.full((2,), 3),
                    torch.arange(2),
                    torch.ones(2, dtype=torch.bool),
                )
        store.verify_alive(3, 2)
        save = dict(
            rid="A",
            op_id=1,
            slot=3,
            prompt_tokens=2,
            source_pages=[0],
            target_pages=[0],
        )
        store.admit(**save)
        events[-1].done = True
        store.admit(**save)
        store.release_source("A")
        store.begin_request("B", 5, 2)
        buffers = [torch.zeros((8, 1, 2), dtype=torch.bfloat16) for _ in range(5)]
        pool = native_methods(
            SimpleNamespace(
                page_size=2,
                start_layer=0,
                layer_num=2,
                kv_lora_rank=2,
                qk_rope_head_dim=2,
                indexer_layer_ids=(0,),
                index_head_dim=2,
                index_k_scale_buffer=None,
                get_key_buffer=lambda i: buffers[i],
                get_value_buffer=lambda i: buffers[2 + i],
                get_index_k_buffer=lambda i: buffers[4],
            )
        )
        with self.assertRaisesRegex(RuntimeError, "private prefix replica"):
            store.load_back(
                rid="B",
                op_id=2,
                slot=5,
                source_pages=[0],
                indices=torch.tensor([2, 3]),
                pool=pool,
            )
        with self.assertRaisesRegex(RuntimeError, "pending or failed"):
            store.release_source("B")

    def test_each_page_read_once_and_every_rank_restores_both_regions(self):
        for assignment in ("round_robin", "contiguous"):
            for count in (1, 31, 32):
                with self.subTest(assignment=assignment, pages=count):
                    self.restore_group(count, assignment)

    def restore_group(self, count, assignment):
        size, page_size = 16, 128
        collective = TensorCollective(size)
        states = []
        sources = list(reversed(range(count)))
        indices = torch.cat(
            (
                torch.arange(page_size, 2 * page_size),
                torch.arange(3 * page_size, (count + 2) * page_size),
            )
        )
        # A gap before the restored rows must remain untouched.
        expected = torch.arange(count * page_size, dtype=torch.bfloat16)
        for rank in range(size):
            helper = test_prefix.TestPrefixStore()
            self.addCleanup(helper.doCleanups)
            scatters = []

            def scatter(*args, calls=scatters):
                calls.append((args[0].untyped_storage().data_ptr(), args[1]))
                test_prefix.cpu_scatter_rows(*args)

            store, manager, kernel, events = helper.make_store(
                pages=count,
                page_size=page_size,
                prompt_capacity=max(8, count * page_size),
                replica=rank,
                readers=size,
                read_assignment=assignment,
                all_gather=collective.rank(rank),
                scatter_kernel=scatter,
            )
            for region, layers in (("main", 2), ("index", 1)):
                for layer in range(layers):
                    alive = kernel.buffers[
                        manager.view(0, layer, region="alive_" + region).device_base
                    ]
                    alive[3, : count * page_size] = expected[:, None, None] + 50 * layer
            save = dict(
                rid="A",
                op_id=1,
                slot=3,
                prompt_tokens=count * page_size,
                source_pages=list(range(count)),
                target_pages=sources,
            )
            store.admit(**save)
            events[-1].done = True
            store.admit(**save)
            store.release_source("A")
            copied = []

            def copy(*args, original=kernel, reads=copied):
                reads.extend(args[2][: args[8]][args[4][: args[8]]].tolist())
                original(*args)

            store._kernel = copy  # Observe actual traffic at the NPU primitive.
            buffers = [
                torch.full(((count + 2) * page_size, 1, 2), -9, dtype=torch.bfloat16)
                for _ in range(5)
            ]
            pool = native_methods(
                SimpleNamespace(
                    page_size=page_size,
                    start_layer=0,
                    layer_num=2,
                    kv_lora_rank=2,
                    qk_rope_head_dim=2,
                    indexer_layer_ids=(0,),
                    index_head_dim=2,
                    index_k_scale_buffer=None,
                    get_key_buffer=lambda i, a=buffers: a[i],
                    get_value_buffer=lambda i, a=buffers: a[2 + i],
                    get_index_k_buffer=lambda i, a=buffers: a[4],
                )
            )
            read = dict(
                rid="B",
                op_id=2,
                slot=5,
                source_pages=sources,
                indices=indices,
                pool=pool,
            )
            states.append((store, read, events, buffers, copied, scatters))
        with ThreadPoolExecutor(max_workers=size) as threads:
            results = list(threads.map(lambda s: s[0].load_back(**s[1]), states))
        self.assertEqual(results, [None] * size)
        traffic = []
        for store, read, events, buffers, copied, scatters in states:
            events[-1].done = True
            receipt = store.load_back(**read)
            self.assertTrue(receipt["readable"])
            self.assertEqual(receipt["split_read"]["read_bytes"], len(copied) // 3 * 20)
            self.assertEqual(len({pointer for pointer, _ in scatters}), 2)
            self.assertEqual(len({table.data_ptr() for _, table in scatters}), 1)
            self.assertEqual(scatters[0][1].shape, (2, count * page_size))
            if count == 31:
                self.assertTrue(torch.all(scatters[0][1][0] < 31 * page_size))
            traffic.extend(copied)
            for i, buffer in enumerate(buffers):
                layer = 1 if i in (1, 3) else 0
                self.assertTrue(
                    torch.equal(buffer[indices, 0, 0], expected + 50 * layer)
                )
                self.assertTrue(torch.all(buffer[:4] == -9))
            before = len(copied)
            self.assertEqual(store.load_back(**read), receipt)
            self.assertEqual(len(copied), before)
        self.assertEqual(sorted(traffic), sorted(list(range(count * page_size)) * 3))


if __name__ == "__main__":
    unittest.main()
