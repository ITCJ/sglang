"""Exercise prefix storage through mapped copies and controllable device events."""

import unittest

import torch
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


class TestPrefixStore(unittest.TestCase):
    def make_store(self, *, pages=3, verify=False, page_size=2, prompt_capacity=8):
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

    def test_acceptance_compares_original_alive_prefix_and_retained_values(self):
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
        with self.assertLogs("ascend_mempool.prefix", level="INFO") as logs:
            store.verify_alive(3, 6)
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
        # A later no-save request still rechecks every retained page after free.
        store.begin_request("r2", 3, 6)
        target = kernel.buffers[manager.view(0, 0, region="prefix_index").device_base]
        target[1, 0, 0, 0] += 1
        with self.assertRaisesRegex(RuntimeError, "prefix content mismatch"):
            store.release_source("r2")

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
