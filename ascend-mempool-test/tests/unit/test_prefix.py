"""Exercise prefix storage through mapped copies and controllable device events."""

import json
import unittest
from types import SimpleNamespace

import torch
from test_mempool_native_prefix import native_methods
from test_pool import FakeBM
from test_runtime import ManualEvent

from ascend_mempool.config import MempoolConfig
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.prefix import PrefixStore


class MappedCopy:
    """Replace only the NPU copy primitive with reads/writes to mapped CPU slabs."""

    def __init__(self, manager):
        self.buffers = {}
        for region, (_, layout) in manager.layout.regions(0).items():
            for layer in range(layout.layers):
                view = manager.view(0, layer, region=region)
                self.buffers[view.device_base] = torch.full(
                    layout.shape, -99, dtype=torch.bfloat16
                )
        # The real pool is only NPU-readable; never dereference its VA on CPU.
        manager._bm.BmMemType.LOCAL_HOST = "local_host"
        translate = manager._handle.gva_to_va

        def mapped_address(gva, kind):
            if kind == "local_host":
                raise AssertionError("BM address is not CPU-readable")
            return translate(gva, kind)

        manager._handle.gva_to_va = mapped_address

    def __call__(
        self,
        src,
        dst,
        src_indices,
        dst_indices,
        valid,
        src_rows,
        dst_rows,
        row_bytes,
        count,
        block_dim,
        src_ptr,
        dst_ptr,
    ):
        source = self.buffers[src_ptr] if src_ptr is not None else src
        target = self.buffers[dst_ptr] if dst_ptr is not None else dst
        source = source.reshape(src_rows, row_bytes // 2)
        target = target.reshape(dst_rows, row_bytes // 2)
        target[dst_indices[:count][valid[:count]]] = source[
            src_indices[:count][valid[:count]]
        ]


def cpu_scatter_rows(
    source, table, key, rope, key_dim, rope_dim, page_size, nz, timing
):
    """CPU device substitute; native readers independently check the layout."""
    source_rows, destination = table
    values = source.view(-1, key_dim + rope_dim)[source_rows]
    for buffer, dim, start in ((key, key_dim, 0), (rope, rope_dim, key_dim)):
        if buffer is None:
            continue
        if nz:
            view = buffer.view(-1, dim // 16, page_size, 16).permute(0, 2, 1, 3)
            view[destination // page_size, destination % page_size] = values[
                :, start : start + dim
            ].reshape(-1, dim // 16, 16)
        else:
            buffer.view(-1, dim)[destination] = values[:, start : start + dim]


class TestPrefixStore(unittest.TestCase):
    def test_reuse_evidence_joins_saved_prefix_and_computed_suffix_after_native_release(
        self,
    ):
        store, manager, kernel, events = self.make_store(verify=True)

        def copies_without_diagnostic_reads(*args):
            self.assertIsNotNone(args[11], "verification must not read BM DRAM")
            kernel(*args)

        store._kernel = copies_without_diagnostic_reads
        store.begin_request("A", 3, 4)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                target = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                values = torch.full_like(target[3, :4], 17 + layer)
                store.capture_source(
                    region,
                    layer,
                    values,
                    torch.full((4,), 3),
                    torch.arange(4),
                    torch.ones(4, dtype=torch.bool),
                )
                target[3, :4] = values
        store.verify_source(3, 4)
        save = dict(
            rid="A",
            op_id=1,
            slot=3,
            prompt_tokens=4,
            source_pages=[0, 1],
            target_pages=[2, 0],
        )
        store.admit(**save)
        events[-1].done = True
        store.admit(**save)
        store.release_source("A")
        store.begin_request("B", 5, 6)
        fill = dict(rid="B", op_id=2, slot=5, source_pages=[2, 0])
        store.fill(**fill)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                target = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                values = torch.full_like(target[5, 4:6], 91 + layer)
                store.capture_source(
                    region,
                    layer,
                    values,
                    torch.full((2,), 5),
                    torch.arange(4, 6),
                    torch.ones(2, dtype=torch.bool),
                )
                target[5, 4:6] = values
        with self.assertLogs("ascend_mempool.prefix", level="INFO") as logs:
            store.verify_source(5, 2, start=4)
            # Native buffers can now be gone; fill uses retained prefix references.
            events[-1].done = True
            store.fill(**fill)
            store.verify_source(5, 6)
        self.assertEqual(len(logs.output), 3)
        store.release_source("B")

    def test_load_back_restores_split_native_buffers_at_discontiguous_token_indices(
        self,
    ):
        store, manager, kernel, events = self.make_store(verify=True)
        store.begin_request("A", 3, 4)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                values = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                values[3, :4] = (
                    torch.arange(4 * values.shape[-1]).reshape(4, 1, -1) + layer * 20
                )
                store.capture_source(
                    region,
                    layer,
                    values[3, :4],
                    torch.full((4,), 3),
                    torch.arange(4),
                    torch.ones(4, dtype=torch.bool),
                )
        store.verify_source(3, 4)
        save = dict(
            rid="A",
            op_id=1,
            slot=3,
            prompt_tokens=4,
            source_pages=[0, 1],
            target_pages=[2, 0],
        )
        store.admit(**save)
        events[-1].done = True
        store.admit(**save)
        store.release_source("A")
        store.begin_request("B", 5, 4)
        k = torch.full((2, 10, 1, 2), -9, dtype=torch.bfloat16)
        rope = torch.full_like(k, -9)
        index = torch.full((10, 1, 2), -9, dtype=torch.bfloat16)
        pool = SimpleNamespace(
            start_layer=4,
            layer_num=2,
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            page_size=2,
            indexer_layer_ids=(4,),
            index_head_dim=2,
            index_k_scale_buffer=None,
            get_key_buffer=lambda layer: k[layer - 4],
            get_value_buffer=lambda layer: rope[layer - 4],
            get_index_k_buffer=lambda layer: index,
        )
        indices = torch.tensor([6, 7, 2, 3])
        pool = native_methods(pool)
        read = dict(
            rid="B", op_id=2, slot=5, source_pages=[2, 0], indices=indices, pool=pool
        )
        read_buffers, scatter_buffers, copied_bytes = set(), set(), []

        def observe_copy(*args):
            read_buffers.add(args[1].data_ptr())
            copied_bytes.append(args[7] * args[8])
            kernel(*args)

        def observe_scatter(*args):
            scatter_buffers.add(args[0].data_ptr())
            cpu_scatter_rows(*args)

        store._kernel, store._scatter_kernel = observe_copy, observe_scatter
        self.assertIsNone(store.load_back(**read))
        with self.assertRaisesRegex(RuntimeError, "pending"):
            store.release_source("B")
        events[-1].done = True
        receipt = store.load_back(**read)
        for flag in (
            "gathered_verified",
            "native_verified",
        ):
            self.assertTrue(receipt["split_read"][flag])
        self.assertEqual(receipt["split_read"]["oracle"], "computed_source")
        self.assertNotIn("private_replica_verified", receipt["split_read"])
        self.assertEqual(len(scatter_buffers), 2)
        self.assertEqual(read_buffers, scatter_buffers)
        self.assertEqual(receipt["split_read"]["read_bytes"], 80)
        self.assertEqual(receipt["split_read"]["oracle_read_bytes"], 0)
        self.assertEqual(sum(copied_bytes), 80)
        self.assertEqual(receipt["prefix_indices"], [6, 7, 2, 3])
        self.assertEqual(receipt["tokens"], 4)
        self.assertEqual(k[0, indices, 0].tolist(), [[0, 1], [4, 5], [8, 9], [12, 13]])
        self.assertEqual(
            rope[0, indices, 0].tolist(), [[2, 3], [6, 7], [10, 11], [14, 15]]
        )
        self.assertEqual(index[indices, 0].tolist(), [[0, 1], [2, 3], [4, 5], [6, 7]])
        self.assertTrue(torch.all(k[:, [0, 1, 4, 5, 8, 9]] == -9))
        self.assertEqual(store.load_back(**read), receipt)
        store._kernel = kernel
        store.release_source("B")

    def test_fill_reads_saved_pages_into_a_new_alive_slot_and_waits_for_completion(
        self,
    ):
        store, manager, kernel, events = self.make_store()
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                alive = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                alive[3, :2] = 11 + layer
                alive[3, 2:4] = 21 + layer
        save = dict(
            rid="A",
            op_id=1,
            slot=3,
            prompt_tokens=4,
            source_pages=[0, 1],
            target_pages=[2, 0],
        )
        self.assertIsNone(store.admit(**save))
        events[-1].done = True
        store.admit(**save)
        store.release_source("A")
        fill = dict(rid="B", op_id=2, slot=5, source_pages=[2, 0])
        self.assertIsNone(store.fill(**fill))
        with self.assertRaisesRegex(RuntimeError, "pending"):
            store.release_source("B")
        events[-1].done = True
        receipt = store.fill(**fill)
        self.assertEqual(receipt["tokens"], 4)
        self.assertEqual(store.fill(**fill), receipt)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                alive = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                self.assertTrue(torch.all(alive[5, :2] == 11 + layer))
                self.assertTrue(torch.all(alive[5, 2:4] == 21 + layer))
                self.assertTrue(torch.all(alive[5, 4:] == -99))
        store.release_source("B")
        self.assertEqual(store.free_pages, (1,))

    def make_store(
        self, *, pages=3, verify=False, page_size=2, prompt_capacity=8, **kwargs
    ):
        layout = MempoolConfig(
            prefill_capacity=prompt_capacity,
            decode_capacity=page_size,
            page_size=page_size,
        ).make_mla_layout(
            num_layers=2,
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            prefix_pages=pages,
            indexer_layers=1,
            index_head_dim=2,
        )
        manager = MempoolKVManager.create(layout, 0, bm_module=FakeBM(0))
        manager.join(0.1)
        self.addCleanup(manager.close, drain=lambda: None)
        kernel = MappedCopy(manager)
        events = []

        def event_factory():
            event = ManualEvent()
            events.append(event)
            return event

        store = PrefixStore(
            manager,
            page_size=page_size,
            device="cpu",
            kernel=kernel,
            event_factory=event_factory,
            verify=verify,
            scatter_kernel=kwargs.pop("scatter_kernel", cpu_scatter_rows),
            **kwargs,
        )
        return store, manager, kernel, events

    def test_admit_copies_both_regions_and_holds_source_until_device_completion(self):
        store, manager, kernel, events = self.make_store()
        references = {}
        for region, layers in (("alive_main", 2), ("alive_index", 1)):
            for layer in range(layers):
                source = kernel.buffers[
                    manager.view(0, layer, region=region).device_base
                ]
                source[3] = (
                    torch.arange(source[3].numel()).reshape(source[3].shape)
                    + 50 * layer
                )
                references[region, layer] = source[3].clone()
        operation = dict(
            rid="r1",
            op_id=7,
            slot=3,
            prompt_tokens=6,
            source_pages=[2, 0],
            target_pages=[2, 0],
        )
        self.assertIsNone(store.admit(**operation))
        self.assertEqual(store.free_pages, (1,))
        with self.assertRaisesRegex(RuntimeError, "pending"):
            store.assert_source_releasable("r1")
        events[-1].done = True
        receipt = store.admit(**operation)
        self.assertTrue(receipt["readable"])
        self.assertEqual(receipt["main_layers"], 2)
        self.assertEqual(receipt["index_layers"], 1)
        self.assertEqual(receipt["target_pages"], [2, 0])
        self.assertEqual(store.admit(**operation), receipt)
        store.release_source("r1")
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ].zero_()
                target = kernel.buffers[
                    manager.view(0, layer, region="prefix_" + region).device_base
                ]
                expected = references["alive_" + region, layer]
                self.assertTrue(torch.equal(target[2], expected[4:6]))
                self.assertTrue(torch.equal(target[0], expected[:2]))
                self.assertTrue(torch.all(target[1] == -99))

    def test_conflicting_operation_and_unavailable_pages_do_not_reuse_storage(self):
        store, _, _, events = self.make_store()
        operation = dict(
            rid="r1",
            op_id=7,
            slot=3,
            prompt_tokens=6,
            source_pages=[0],
            target_pages=[0],
        )
        self.assertIsNone(store.admit(**operation))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            store.admit(**dict(operation, source_pages=[1]))
        with self.assertRaisesRegex(ValueError, "not free"):
            store.admit(**dict(operation, op_id=8))
        self.assertEqual(store.free_pages, (1, 2))
        with self.assertRaisesRegex(RuntimeError, "pending"):
            store.release_source("r1")
        events[-1].done = True
        store.admit(**operation)
        store.release_source("r1")
        self.assertEqual(store.free_pages, (1, 2))

    def test_source_snapshots_survive_source_mutation_and_release(self):
        store, manager, kernel, events = self.make_store(verify=True)
        store.begin_request("r1", 3, 6)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                alive = kernel.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                values = (
                    torch.arange(6 * alive.shape[-1])
                    .reshape(6, 1, -1)
                    .to(torch.bfloat16)
                    + layer * 10
                )
                store.capture_source(
                    region,
                    layer,
                    values,
                    torch.full((6,), 3),
                    torch.arange(6),
                    torch.ones(6, dtype=torch.bool),
                )
                alive[3, :6] = values
                values.zero_()
        with self.assertLogs("ascend_mempool.prefix", level="INFO") as logs:
            store.verify_source(3, 6)
            operation = dict(
                rid="r1",
                op_id=7,
                slot=3,
                prompt_tokens=6,
                source_pages=[2, 0],
                target_pages=[1, 2],
            )
            self.assertIsNone(store.admit(**operation))
            events[-1].done = True
            store.admit(**operation)
            for region, layers in (("main", 2), ("index", 1)):
                for layer in range(layers):
                    kernel.buffers[
                        manager.view(0, layer, region="alive_" + region).device_base
                    ].zero_()
            store.release_source("r1")
        self.assertEqual(len(logs.output), 3)
        reports = [json.loads(line.split("data=", 1)[1]) for line in logs.output]
        self.assertTrue(all(r["content_source"] == "computed_source" for r in reports))
        self.assertTrue(all(r["bm_readback"] is False for r in reports))
        self.assertEqual(
            reports[1]["pages"],
            {"1": reports[0]["pages"]["2"], "2": reports[0]["pages"]["0"]},
        )
        self.assertEqual(reports[2]["pages"], reports[1]["pages"])
        # Unused DRAM corruption is outside this verification's coverage.
        store.begin_request("r2", 3, 6)
        target = kernel.buffers[manager.view(0, 0, region="prefix_index").device_base]
        target[1, 0, 0, 0] += 1
        with self.assertLogs("ascend_mempool.prefix", level="INFO") as later:
            store.release_source("r2")
        retained = json.loads(later.output[0].split("data=", 1)[1])
        self.assertEqual(retained["pages"], reports[2]["pages"])
        self.assertFalse(retained["bm_readback"])

    def test_copy_failure_retains_target_and_prevents_source_release(self):
        store, _, _, _ = self.make_store()

        def failure(*args):
            raise RuntimeError("device copy failure")

        store._kernel = (
            failure  # External NPU primitive fails after target reservation.
        )
        with self.assertRaisesRegex(RuntimeError, "device copy failure"):
            store.admit(
                rid="r1",
                op_id=7,
                slot=3,
                prompt_tokens=6,
                source_pages=[0],
                target_pages=[0],
            )
        self.assertEqual(store.free_pages, (1, 2))
        with self.assertRaisesRegex(RuntimeError, "pending"):
            store.release_source("r1")


if __name__ == "__main__":
    unittest.main()
