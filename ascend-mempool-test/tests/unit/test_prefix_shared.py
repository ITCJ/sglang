"""Single-copy prefix through real stores and mapped CPU copy primitives."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import torch
from test_prefix import MappedCopy, cpu_scatter_rows
from test_prefix_split_read import TensorCollective
from test_runtime import ManualEvent
from test_sharing_pool import GroupSDK

from ascend_mempool.config import MempoolConfig
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.page_write import PageWriter
from ascend_mempool.prefix import PrefixStore
from ascend_mempool.prefix_layout import PrefixLayout
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig


class TestSharedPrefix(unittest.TestCase):
    def test_exhausted_segment_uses_most_free_then_stable_segment_order(self):
        geometry = PrefixLayout(40, tuple((r, r + 1) for r in range(0, 16, 2)))
        self.assertEqual(
            geometry.select(set(range(5, 40)), 8, 0), [5, 6, 10, 15, 20, 25, 30, 35]
        )
        self.assertEqual(geometry.select({0, 1, 5, 10, 11}, 5, 7), [0, 1, 5, 10, 11])

    def group(self, mode="owners", page_size=2, capacity=80, **options):
        collective = options.pop("collective", None)
        layout = MempoolConfig(
            prefill_capacity=capacity, decode_capacity=page_size, page_size=page_size
        ).make_mla_layout(
            num_layers=2,
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            prefix_pages=40,
            indexer_layers=1,
            index_head_dim=2,
        )
        plan = MempoolNumaConfig(False, "0,1,2,3,4,5,6,7").plan(
            hosts=(HostTopology("p", tuple(range(16)), "0-7"),),
            tp_size=16,
            role="prefill",
            instance_id="prefix-cpu",
            kv_identity="model",
            layout=layout,
            prefill_shared=True,
            decode_shared=True,
            prefill_index_shared=True,
            prefix_shared=True,
        )
        memory, states = {}, []
        for rank in range(16):
            session = plan.session(rank)
            manager = MempoolKVManager.create(
                session.layout, 0, session=session, bm_module=GroupSDK(rank)
            )
            self.addCleanup(manager.close, drain=lambda: None)
            manager.join(0.1)
            kernel = MappedCopy(manager)
            for ptr, buffer in kernel.buffers.items():
                kernel.buffers[ptr] = memory.setdefault(ptr, buffer)
            events = []

            def event_factory(events=events):
                event = ManualEvent()
                events.append(event)
                return event

            store = PrefixStore(
                manager,
                page_size=page_size,
                device="cpu",
                kernel=kernel,
                event_factory=event_factory,
                replica=rank,
                page_writer=PageWriter(rank % 2, 2, page_size),
                index_writer=PageWriter(0 if rank == 0 else -1, 1, page_size),
                readers=16 if collective else 1,
                all_gather=collective.rank(rank) if collective else None,
                scatter_kernel=cpu_scatter_rows,
                admit_mode=mode,
                **options,
            )
            states.append((store, manager, kernel, events))
        return states, memory

    def populate(self, states, tokens=64, slot=0, rid="A"):
        for rank, (store, manager, kernel, _) in enumerate(states):
            store.begin_request(rid, slot, tokens)
            positions = torch.arange(tokens)
            for region, layers, writer in (
                ("main", 2, store.page_writer),
                ("index", 1, store.index_writer),
            ):
                mask = writer.mask(positions, torch.full_like(positions, tokens))
                for layer in range(layers):
                    values = (
                        positions // 2
                        + (rank * 4 + layer * 80 if region == "main" else 100)
                    ).to(torch.bfloat16)
                    alive = kernel.buffers[
                        manager.view(0, layer, region="alive_" + region).device_base
                    ]
                    alive[slot, :tokens][mask] = values[mask, None, None]
                    store.capture_source(
                        region,
                        layer,
                        values[:, None, None].expand(-1, 1, alive.shape[-1]),
                        torch.full((tokens,), slot),
                        positions,
                        torch.ones(tokens, dtype=torch.bool),
                    )
            store.verify_source(slot, tokens)

    def test_interleaved_admit_uses_each_owner_source_and_waits_for_all_writers(self):
        for mode in ("owners", "single_rank"):
            with self.subTest(mode=mode):
                states, memory = self.group(mode)
                self.populate(states)
                targets = states[0][0].select_pages(32)
                self.assertEqual(
                    targets[:16],
                    [0, 5, 10, 15, 20, 25, 30, 35, 1, 6, 11, 16, 21, 26, 31, 36],
                )
                save = dict(
                    rid="A",
                    op_id=1,
                    slot=0,
                    prompt_tokens=64,
                    source_pages=list(range(32)),
                    target_pages=targets,
                )
                receipts = []
                for store, _, _, events in states:
                    self.assertEqual(store.select_pages(32), targets)
                    self.assertIsNone(store.admit(**save))
                    events[-1].done = True
                    receipts.append(store.admit(**save))
                self.assertEqual(
                    sorted(p for r in receipts for p in r["written_pages"]),
                    list(p for p in sorted(targets)),
                )
                with self.assertRaisesRegex(RuntimeError, "pending or failed"):
                    states[0][0].release_source("A")
                bad = [dict(r) for r in receipts]
                bad[0] = dict(
                    bad[0], written_pages=bad[0]["written_pages"] + [targets[-1]]
                )
                with self.assertRaisesRegex(ValueError, "writer"):
                    states[0][0].complete_admit(1, bad)
                for store, manager, _, _ in states:
                    store.complete_admit(1, receipts)
                    for source, physical in enumerate(targets):
                        segment, offset = divmod(physical, 5)
                        source_rank = (segment * 2 if mode == "owners" else 0) + (
                            source >= 16
                        )
                        for layer in range(2):
                            saved = memory[
                                manager.view(
                                    0, layer, region="prefix_main", segment=segment
                                ).device_base
                            ]
                            self.assertTrue(
                                torch.all(
                                    saved[offset]
                                    == source_rank * 4 + source + layer * 80
                                )
                            )
                        saved = memory[
                            manager.view(
                                0, 0, region="prefix_index", segment=segment
                            ).device_base
                        ]
                        self.assertTrue(torch.all(saved[offset] == 100 + source))
                    store.release_source("A")
                    store.begin_request("B", 1, 80)
                    self.assertEqual(
                        store.select_pages(8), [9, 14, 19, 24, 29, 34, 39, 4]
                    )

    def test_segment_reads_once_and_direct_fill_restores_eight_main_replicas(self):
        for count in (1, 31, 32):
            with self.subTest(pages=count):
                states, memory = self.group(
                    collective=TensorCollective(16),
                    read_assignment="by_segment",
                    verify=True,
                    start_segment=3,
                )
                self.populate(states, tokens=count * 2)
                targets = states[0][0].select_pages(count)
                save = dict(
                    rid="A",
                    op_id=1,
                    slot=0,
                    prompt_tokens=count * 2,
                    source_pages=list(range(count)),
                    target_pages=targets,
                )
                receipts = []
                for store, _, _, events in states:
                    store.admit(**save)
                    events[-1].done = True
                    receipts.append(store.admit(**save))
                reads, traffic = [], []
                indices = torch.cat(
                    [torch.arange(4 + 4 * p, 6 + 4 * p) for p in range(count)]
                )
                for store, manager, kernel, events in states:
                    store.complete_admit(1, receipts)
                    store.release_source("A")
                    store.begin_request("B", 1, 80)

                    def copy(*args, kernel=kernel):
                        traffic.append((args[11] is None, args[7] * int(args[4].sum())))
                        kernel(*args)

                    store._kernel = copy
                    buffers = [
                        torch.full((4 * (count + 1), 1, 2), -9, dtype=torch.bfloat16)
                        for _ in range(5)
                    ]
                    pool = SimpleNamespace(
                        page_size=2,
                        start_layer=0,
                        layer_num=2,
                        kv_lora_rank=2,
                        qk_rope_head_dim=2,
                        indexer_layer_ids=(0,),
                        index_head_dim=2,
                        index_k_scale_buffer=None,
                        use_fia_nz=False,
                        get_key_buffer=lambda i, b=buffers: b[i],
                        get_value_buffer=lambda i, b=buffers: b[2 + i],
                        get_index_k_buffer=lambda i, b=buffers: b[4],
                    )
                    reads.append(
                        dict(
                            rid="B",
                            op_id=2,
                            slot=1,
                            source_pages=targets,
                            indices=indices,
                            pool=pool,
                        )
                    )
                with ThreadPoolExecutor(max_workers=16) as threads:
                    results = list(
                        threads.map(
                            lambda pair: pair[0][0].load_back(**pair[1]),
                            zip(states, reads),
                        )
                    )
                self.assertEqual(results, [None] * 16)
                self.assertEqual(
                    sum(size for load, size in traffic if load), count * 40
                )
                expected = torch.tensor(
                    [
                        ((p + 3) % 8 * 2 + (p >= (count + 1) // 2)) * 4 + p
                        for p in range(count)
                    ],
                    dtype=torch.bfloat16,
                ).repeat_interleave(2)
                load_receipts, fill_receipts = [], []
                for rank, ((store, manager, _, events), read) in enumerate(
                    zip(states, reads)
                ):
                    events[-1].done = True
                    if rank == 0:
                        buffer = read["pool"].get_key_buffer(0)
                        buffer[indices[0]] += 1
                        with self.assertRaisesRegex(RuntimeError, "source writers"):
                            store.load_back(**read)
                        buffer[indices[0]] -= 1
                    load_receipts.append(store.load_back(**read))
                    self.assertTrue(load_receipts[-1]["readable"])
                    for layer in (0, 1):
                        for buffer in (
                            read["pool"].get_key_buffer(layer),
                            read["pool"].get_value_buffer(layer),
                        ):
                            self.assertTrue(
                                torch.equal(
                                    buffer[indices, 0, 0], expected + layer * 80
                                )
                            )
                            self.assertTrue(torch.all(buffer[:4] == -9))
                    self.assertEqual(
                        read["pool"].get_index_k_buffer(0)[indices, 0, 0].tolist(),
                        [100 + p for p in range(count) for _ in range(2)],
                    )
                    fill = dict(rid="B", op_id=3, slot=1, source_pages=targets)
                    self.assertIsNone(store.fill(**fill))
                    events[-1].done = True
                    fill_receipts.append(store.fill(**fill))
                    self.assertTrue(fill_receipts[-1]["readable"])
                self.assertEqual(
                    sum(size for load, size in traffic if not load), count * 264
                )
                for _, manager, _, _ in states:
                    for layer in (0, 1):
                        alive = memory[manager.view(0, layer).device_base]
                        self.assertTrue(
                            torch.equal(
                                alive[1, : count * 2, 0, 0], expected + layer * 80
                            )
                        )
                        self.assertTrue(torch.all(alive[1, count * 2 :] == -99))
                from verify_mempool_shared import check_prefix_operations

                config = SimpleNamespace(
                    prefix_pages=40,
                    prefill_groups=tuple((r, r + 1) for r in range(0, 16, 2)),
                    prefix_start_segment=3,
                    prefix_admit_rank=0,
                    prefix_admit_mode="owners",
                    page_size=2,
                    prefill_index_writers=(0,),
                    prefill_write_assignment="contiguous",
                    read_assignment="by_segment",
                )
                snapshot = dict(
                    registrations={
                        "prefill": {
                            "ranks": [
                                dict(layers=2, dim=4, indexer_layers=[0], index_dim=2)
                            ]
                        }
                    },
                    completed=[
                        dict(
                            admit=dict(command=save, ack=dict(ranks=receipts)), reads={}
                        )
                    ],
                )
                free = set(range(40))
                check_prefix_operations(snapshot, config, count * 2, 0, free)
                done = snapshot["completed"][-1]
                done["admit"] = None
                done["reads"] = {
                    kind: dict(
                        command=dict(
                            sources=[
                                dict(pages=[dict(logical_page=p) for p in targets])
                            ]
                        ),
                        ack=dict(ranks=actual),
                    )
                    for kind, actual in (
                        ("load_back", load_receipts),
                        ("fill", fill_receipts),
                    )
                }
                evidence = check_prefix_operations(snapshot, config, 80, 1, free)
                self.assertEqual(evidence["fill"]["main_bytes"], 8 * count * 32)
                load_receipts[0]["main_bytes"] += 1
                with self.assertRaisesRegex(ValueError, "byte"):
                    check_prefix_operations(snapshot, config, 80, 1, free)
