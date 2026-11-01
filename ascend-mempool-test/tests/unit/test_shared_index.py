"""Single Index K storage through model write, admit and fill interfaces."""

import unittest
from types import SimpleNamespace

import torch
from test_offload import CPUWriteKernel
from test_prefix import MappedCopy
from test_runtime import ManualEvent, prefill_batch
from test_sharing_pool import GroupSDK

from ascend_mempool.config import MempoolConfig
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.page_write import PageWriter
from ascend_mempool.prefix import PrefixStore
from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig


class TestSharedIndex(unittest.TestCase):
    def test_ready_waits_for_index_owner_with_no_main_pages(self):
        from ascend_mempool_pd.mempool_accounting import PromptCoverage

        coverage = PromptCoverage(
            256,
            128,
            128,
            4,
            ((0, 1), (2, 3)),
            ((), (0, 1), (), (0, 1)),
            index_writer_pages=((0, 1), (), (), ()),
        )
        for rank in (1, 2, 3):
            coverage.observe(rank, 128, 256, True)
        coverage.observe(0, 128, 128, False)
        self.assertEqual(coverage.snapshot()["completed_pages"], [2, 2])
        self.assertEqual(coverage.snapshot()["index_completed_pages"], 0)
        self.assertEqual(coverage.snapshot()["ready_tokens"], 0)
        coverage.observe(0, 128, 256, False)
        self.assertEqual(coverage.snapshot()["ready_tokens"], 0)
        coverage.observe(0, 128, 256, True)
        self.assertEqual(coverage.snapshot()["index_completed_pages"], 2)
        self.assertEqual(coverage.snapshot()["ready_tokens"], 256)

    def test_only_assigned_writers_fill_one_index_and_all_shards_admit_it(self):
        for index_writers, expected_index in (
            ((0,), [70] * 6),
            ((0, 1), [70] * 4 + [71] * 2),
        ):
            with self.subTest(writers=index_writers):
                self.exercise(index_writers, expected_index)

    def exercise(self, index_writers, expected_index):
        layout = MempoolConfig(
            prefill_capacity=8, decode_capacity=2, page_size=2
        ).make_mla_layout(
            num_layers=2,
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            prefix_pages=3,
            indexer_layers=1,
            index_head_dim=2,
        )
        plan = MempoolNumaConfig(False, "0,1").plan(
            hosts=(HostTopology("p", (0, 1, 2, 3), "0-1"),),
            tp_size=4,
            role="prefill",
            instance_id="index-cpu",
            kv_identity="model",
            layout=layout,
            prefill_shared=True,
            prefill_index_shared=True,
        )
        memory, participants, written = {}, [], []
        for rank in range(4):
            session = plan.session(rank)
            pool = MempoolKVManager.create(
                session.layout, 0, session=session, bm_module=GroupSDK(rank)
            )
            self.addCleanup(pool.close, drain=lambda: None)
            pool.join(0.1)
            kernel = MappedCopy(pool)
            for ptr, buffer in kernel.buffers.items():
                kernel.buffers[ptr] = memory.setdefault(ptr, buffer)
            events = []

            def event_factory(events=events):
                event = ManualEvent()
                events.append(event)
                return event

            main_writer = PageWriter(rank % 2, 2, 2)
            index_writer = PageWriter(
                index_writers.index(rank) if rank in index_writers else -1,
                len(index_writers),
                2,
            )
            store = PrefixStore(
                pool,
                page_size=2,
                device="cpu",
                kernel=kernel,
                event_factory=event_factory,
                page_writer=main_writer,
                index_writer=index_writer,
            )
            native = torch.full((6, 1, 2), 70 + rank, dtype=torch.bfloat16)
            runtime = MempoolRuntime(
                pool,
                req_pool_rows=3,
                max_context_len=8,
                start_layer=0,
                device="cpu",
                kernels=[
                    CPUWriteKernel(
                        memory[pool.view(0, i).device_base], pool.view(0, i).device_base
                    )
                    for i in range(2)
                ],
                index_pool=SimpleNamespace(
                    indexer_layer_ids=(0,),
                    index_head_dim=2,
                    index_k_scale_buffer=None,
                    get_index_k_buffer=lambda layer, native=native: native,
                ),
                index_kernels=[
                    CPUWriteKernel(
                        memory[pool.view(0, 0, region="alive_index").device_base],
                        pool.view(0, 0, region="alive_index").device_base,
                    )
                ],
                event_factory=event_factory,
                page_writer=main_writer,
                index_writer=index_writer,
                prefix_store=store,
            )
            store.begin_request("A", 0, 6)
            binding = runtime.bind(1, slot=0, prompt_tokens=6)
            values = torch.full((6, 2), 10 + rank, dtype=torch.bfloat16)
            runtime.begin_forward([KVWriteExpectation(1, 0, 6)])
            for layer in (0, 1):
                runtime.write_layer(
                    layer, values, values, prefill_batch((6,), (0,), (1,))
                )
            runtime.end_forward()
            events[-1].done = True
            runtime.poll_completed()
            written.append(runtime.detach_row(binding).index_written)
            participants.append((pool, store, events))
        self.assertEqual(
            written, [6, 0, 0, 0] if len(index_writers) == 1 else [4, 2, 0, 0]
        )
        for pool, store, events in participants:
            save = dict(
                rid="A",
                op_id=1,
                slot=0,
                prompt_tokens=6,
                source_pages=[0, 1, 2],
                target_pages=[2, 0, 1],
            )
            self.assertIsNone(store.admit(**save))
            events[-1].done = True
            self.assertTrue(store.admit(**save)["readable"])
            saved = memory[pool.view(0, 0, region="prefix_index").device_base]
            self.assertEqual(
                saved[[2, 0, 1], :, 0, 0].flatten().tolist(), expected_index
            )
            store.release_source("A")
            store.begin_request("B", 1, 6)
            fill = dict(rid="B", op_id=2, slot=1, source_pages=[2, 0, 1])
            self.assertIsNone(store.fill(**fill))
            events[-1].done = True
            self.assertEqual(
                store.fill(**fill)["index_rows"], written[pool.session.tp_rank]
            )
        index = memory[participants[0][0].view(0, 0, region="alive_index").device_base]
        self.assertEqual(index[1, :6, 0, 0].tolist(), expected_index)
