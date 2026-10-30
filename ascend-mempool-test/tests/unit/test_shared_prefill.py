"""Exercise shared writes, private Index K, admit and fill through public APIs."""

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


class TestSharedPrefill(unittest.TestCase):
    def test_model_writers_and_prefix_copies_share_only_main_kv(self):
        for policy, expected in (
            ("contiguous", [10, 10, 10, 10, 11, 11]),
            ("round_robin", [10, 10, 11, 11, 10, 10]),
        ):
            with self.subTest(policy=policy):
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
                plan = MempoolNumaConfig(False, "0").plan(
                    hosts=(HostTopology("p", (0, 1), "0"),),
                    tp_size=2,
                    role="prefill",
                    instance_id="shared-cpu",
                    kv_identity="model",
                    layout=layout,
                    prefill_shared=True,
                )
                memory, participants = {}, []
                for rank in (0, 1):
                    session = plan.session(rank)
                    pool = MempoolKVManager.create(
                        session.layout, 0, session=session, bm_module=GroupSDK(rank)
                    )
                    self.addCleanup(pool.close, drain=lambda: None)
                    pool.join(0.1)
                    kernel = MappedCopy(pool)
                    for ptr, buffer in kernel.buffers.items():
                        kernel.buffers[ptr] = memory.setdefault(ptr, buffer)
                    writer = PageWriter(rank, 2, 2, policy)
                    events = []

                    def event_factory(events=events):
                        event = ManualEvent()
                        events.append(event)
                        return event

                    store = PrefixStore(
                        pool,
                        page_size=2,
                        device="cpu",
                        kernel=kernel,
                        event_factory=event_factory,
                        page_writer=writer,
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
                                memory[pool.view(0, i).device_base],
                                pool.view(0, i).device_base,
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
                                memory[
                                    pool.view(0, 0, region="alive_index").device_base
                                ],
                                pool.view(0, 0, region="alive_index").device_base,
                            )
                        ],
                        event_factory=event_factory,
                        page_writer=writer,
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
                    receipt = runtime.detach_row(binding)
                    self.assertEqual(receipt.main_written, 4 if rank == 0 else 2)
                    native.zero_()
                    index = memory[pool.view(0, 0, region="alive_index").device_base]
                    self.assertEqual(index[0, :6, 0, 0].tolist(), [70 + rank] * 6)
                    participants.append((pool, store, events))
                for pool, store, events in participants:
                    self.assertEqual(
                        memory[pool.view(0, 0).device_base][0, :6, 0, 0].tolist(),
                        expected,
                    )
                    save = dict(
                        rid="A",
                        op_id=1,
                        slot=0,
                        prompt_tokens=6,
                        source_pages=[0, 1, 2],
                        target_pages=[2, 0, 1],
                    )
                    store.admit(**save)
                    events[-1].done = True
                    store.admit(**save)
                    store.release_source("A")
                    store.begin_request("B", 1, 6)
                    fill = dict(rid="B", op_id=2, slot=1, source_pages=[2, 0, 1])
                    self.assertIsNone(store.fill(**fill))
                    events[-1].done = True
                    receipt = store.fill(**fill)
                    self.assertEqual(
                        receipt["main_rows"], 4 if pool.session.tp_rank == 0 else 2
                    )
                for rank, (pool, _, _) in enumerate(participants):
                    self.assertEqual(
                        memory[pool.view(0, 0).device_base][1, :6, 0, 0].tolist(),
                        expected,
                    )
                    index = memory[pool.view(0, 0, region="alive_index").device_base]
                    self.assertEqual(index[1, :6, 0, 0].tolist(), [70 + rank] * 6)
