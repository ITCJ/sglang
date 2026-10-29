"""Use real rank-pair ownership with CPU transport/device completion seams."""

import unittest
from array import array
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from test_mempool_native_reservation import Pages, Rows
from test_pd_tick import CPUCollective

from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_native_reservation import NativeReservations
from ascend_mempool_pd.mempool_protocol import PoolDescriptor, PoolPeer
from ascend_mempool_pd.mempool_subrouter import MempoolSubrouter, RouterShard
from ascend_mempool_pd.mempool_tick import RequestObservation


class TestSubrouter(unittest.TestCase):
    def test_quiesce_requires_idle_ranks_then_stops_control_traffic(self):
        self.commands["prefill"].append(
            dict(run_id="cpu", role="prefill", kind="quiesce")
        )
        self.advance("prefill")
        receipt = next(
            m for m in self.messages["prefill"] if m["kind"] == "quiesce_ack"
        )
        self.assertEqual(receipt["ranks"], [dict(rank=i, idle=True) for i in range(16)])
        before = list(self.messages["prefill"])
        for tick in self.ticks["prefill"]:
            self.assertTrue(tick.quiesced)
            tick.clock = lambda: 3600
            tick.receive = lambda: (_ for _ in ()).throw(
                RuntimeError("Main has exited")
            )
        self.advance("prefill")
        self.assertEqual(self.messages["prefill"], before)

    def test_ready_requires_fill_and_the_last_replica_page(self):
        self.request.update(input_ids=[7] * 256, prompt_tokens=256, hit_len=128)
        d = self.command("decode", "alloc", request=self.request)
        p = self.command("prefill", "alloc", request=self.request, bindings=d["ranks"])
        d = self.command(
            "decode", "dispatch", request=self.request, bindings=p["ranks"]
        )
        self.command("prefill", "dispatch", request=self.request, bindings=d["ranks"])
        facts = [
            [
                RequestObservation(
                    1,
                    256,
                    4,
                    100,
                    attempt="attempt",
                    writes_done=True,
                    write_range=(128, 256),
                )
            ]
            for _ in range(16)
        ]
        facts[15] = [
            RequestObservation(
                1, 256, 4, 100, attempt="attempt", write_range=(128, 128)
            )
        ]
        self.advance("prefill", facts)
        self.advance("prefill", facts)
        self.assertFalse(
            any(m["kind"] == "ready_tokens" for m in self.messages["prefill"])
        )
        for tick in self.ticks["prefill"]:
            tick.shard.fill_complete = True
        facts[15] = [
            RequestObservation(
                1, 256, 4, 100, attempt="attempt", write_range=(128, 128)
            )
        ]
        self.advance("prefill", facts)
        self.assertFalse(
            any(m["kind"] == "ready_tokens" for m in self.messages["prefill"])
        )
        facts[15] = facts[0]
        self.advance("prefill", facts)
        ready = next(m for m in self.messages["prefill"] if m["kind"] == "ready_tokens")
        self.assertTrue(
            all(r["message"]["prompt_tokens"] == 256 for r in ready["ranks"])
        )
        accounting = self.ticks["prefill"][0].shard.coverage.snapshot()
        self.assertEqual(accounting["ready_tokens"], 256)
        self.assertEqual(accounting["counter_tokens"], 256)

    def test_decode_free_waits_for_readers_and_reports_zero(self):
        from msgspec.structs import replace

        self.test_late_native_transfer_holds_first_token_and_explicit_free()
        facts = [
            [
                RequestObservation(
                    1,
                    128,
                    4,
                    100,
                    attempt="attempt",
                    release=True,
                    writes_done=True,
                    drained=r != 15,
                    host_drained=r != 15,
                    native_freed=True,
                )
            ]
            for r in range(16)
        ]
        self.advance("decode", facts)
        self.assertEqual(self.ticks["decode"][0].shard.readers.counts, [0] * 15 + [1])
        facts[15] = [replace(facts[15][0], drained=True, host_drained=True)]
        self.advance("decode", facts)
        self.advance("decode", facts)
        self.op += 1
        self.commands["decode"].append(
            dict(
                run_id="cpu",
                role="decode",
                kind="free",
                rid="r1",
                room=1,
                attempt="attempt",
                op_id=self.op,
            )
        )
        self.advance("decode", facts)
        ack = next(m for m in self.messages["decode"] if m["kind"] == "free_ack")
        self.assertEqual([r["readers_remaining"] for r in ack["ranks"]], [0] * 16)

    def test_restore_and_fill_wait_for_last_rank_while_native_release_can_progress(
        self,
    ):
        import test_prefix as prefix_fixture
        import torch
        from test_mempool_native_prefix import native_methods

        from ascend_mempool_pd.mempool_prefill_restore import PrefillRestore

        self.request.update(input_ids=[7] * 256, prompt_tokens=256, hit_len=128)
        events = []
        for tick in self.ticks["prefill"]:
            helper = prefix_fixture.TestPrefixStore()
            self.addCleanup(helper.doCleanups)
            store, _, _, device_events = helper.make_store(
                pages=40, page_size=128, prompt_capacity=8192
            )
            save = dict(
                rid="A",
                op_id=100,
                slot=3,
                prompt_tokens=128,
                source_pages=[0],
                target_pages=[0],
            )
            store.admit(**save)
            device_events[-1].done = True
            store.admit(**save)
            store.release_source("A")
            arrays = [torch.zeros((512, 1, 2), dtype=torch.bfloat16) for _ in range(5)]
            pool = SimpleNamespace(
                page_size=128,
                start_layer=0,
                layer_num=2,
                kv_lora_rank=2,
                qk_rope_head_dim=2,
                indexer_layer_ids=(0,),
                index_head_dim=2,
                index_k_scale_buffer=None,
                get_key_buffer=lambda i, a=arrays: a[i],
                get_value_buffer=lambda i, a=arrays: a[2 + i],
                get_index_k_buffer=lambda i, a=arrays: a[4],
            )
            pool = native_methods(pool)
            pages = Pages()
            pages.get_kvcache = lambda p=pool: p
            tick.shard.prefix, tick.shard.restore = store, PrefillRestore(pages, store)
            events.append(device_events)
        d = self.command("decode", "alloc", request=self.request)
        p = self.command("prefill", "alloc", request=self.request, bindings=d["ranks"])
        d = self.command(
            "decode", "dispatch", request=self.request, bindings=p["ranks"]
        )
        self.command("prefill", "dispatch", request=self.request, bindings=d["ranks"])
        self.op += 1
        command = dict(
            run_id="cpu",
            role="prefill",
            kind="load_back",
            op_id=self.op,
            rid="r1",
            room=1,
            attempt="attempt",
            hit_len=128,
            sources=[
                dict(
                    rank=i,
                    slot=r["slot"],
                    pages=[
                        dict(
                            pool="prefix",
                            region=f"p-{i}",
                            replica=i,
                            logical_page=0,
                            page_size=128,
                        )
                    ],
                )
                for i, r in enumerate(p["ranks"])
            ],
        )
        self.commands["prefill"].append(command)
        self.advance("prefill")
        for ev in events[:15]:
            ev[-1].done = True
        self.advance("prefill")
        self.assertFalse(
            any(m["kind"] == "load_back_ack" for m in self.messages["prefill"])
        )
        events[-1][-1].done = True
        self.advance("prefill")
        self.assertTrue(
            any(m["kind"] == "load_back_ack" for m in self.messages["prefill"])
        )
        facts = [
            [
                RequestObservation(
                    1,
                    256,
                    4,
                    100,
                    attempt="attempt",
                    prompt_ready=True,
                    write_range=(128, 256),
                    writes_done=True,
                    native_release=True,
                )
            ]
            for _ in range(16)
        ]
        self.commands["prefill"].append(dict(command, kind="fill", op_id=self.op + 1))
        self.advance("prefill", facts)
        for ev in events[:15]:
            ev[-1].done = True
        self.advance("prefill", facts)
        self.assertTrue(
            all("native_release" in effects for effects in self.effects["prefill"])
        )
        self.assertFalse(
            any(
                m["kind"] in ("fill_ack", "ready_tokens")
                for m in self.messages["prefill"]
            )
        )
        events[-1][-1].done = True
        self.advance("prefill", facts)
        self.advance("prefill", facts)
        self.assertTrue(any(m["kind"] == "fill_ack" for m in self.messages["prefill"]))
        self.assertTrue(
            any(m["kind"] == "ready_tokens" for m in self.messages["prefill"])
        )

    def test_admit_waits_for_last_rank_before_any_queue_advances_or_source_frees(self):
        import test_prefix as prefix_fixture

        stores, events = [], []
        for tick in self.ticks["prefill"]:
            helper = prefix_fixture.TestPrefixStore()
            self.addCleanup(helper.doCleanups)
            store, _, _, device_events = helper.make_store(
                pages=40, page_size=128, prompt_capacity=8192
            )
            tick.shard.prefix = store
            stores.append(store)
            events.append(device_events)
        d = self.command("decode", "alloc", request=self.request)
        p = self.command("prefill", "alloc", request=self.request, bindings=d["ranks"])
        d = self.command(
            "decode", "dispatch", request=self.request, bindings=p["ranks"]
        )
        self.command("prefill", "dispatch", request=self.request, bindings=d["ranks"])
        facts = [
            [
                RequestObservation(
                    1,
                    128,
                    4,
                    100,
                    attempt="attempt",
                    prompt_ready=True,
                    write_range=(0, 128),
                    writes_done=True,
                )
            ]
            for _ in range(16)
        ]
        self.advance("prefill", facts)
        self.advance("prefill", facts)
        facts = [
            [
                RequestObservation(
                    1,
                    128,
                    4,
                    100,
                    attempt="attempt",
                    prompt_ready=True,
                    write_range=(0, 128),
                    writes_done=True,
                    native_freed=True,
                )
            ]
            for _ in range(16)
        ]
        self.advance("prefill", facts)
        self.op += 1
        command = dict(
            run_id="cpu",
            role="prefill",
            kind="admit",
            rid="r1",
            room=1,
            attempt="attempt",
            op_id=self.op,
            source_pages=[0],
            hashes=["h0"],
            sources=[
                dict(rank=r["rank"], slot=r["slot"], pages=r["pages"][:1])
                for r in p["ranks"]
            ],
        )
        self.commands["prefill"].append(command)
        self.advance("prefill", facts)
        for rank_events in events[:15]:
            rank_events[-1].done = True
        self.advance("prefill", facts)
        self.assertFalse(
            any(m["kind"] == "admit_ack" for m in self.messages["prefill"])
        )
        self.assertTrue(all(len(t.pending) == 1 for t in self.ticks["prefill"]))
        self.assertTrue(
            all(len(c.available_slots()) == 15 for c in self.controls["prefill"])
        )
        self.assertTrue(
            all(store.free_pages == tuple(range(1, 40)) for store in stores)
        )
        events[15][-1].done = True
        self.advance("prefill", facts)
        ack = next(m for m in self.messages["prefill"] if m["kind"] == "admit_ack")
        self.assertEqual(len(ack["ranks"]), 16)
        self.assertTrue(
            all(r["readable"] and r["target_pages"] == [0] for r in ack["ranks"])
        )
        self.assertTrue(all(not t.pending for t in self.ticks["prefill"]))
        self.commands["prefill"].append(dict(command, kind="free", op_id=self.op + 1))
        self.advance("prefill", facts)
        self.assertTrue(
            all(len(c.available_slots()) == 16 for c in self.controls["prefill"])
        )
        self.assertTrue(
            all(store.free_pages == tuple(range(1, 40)) for store in stores)
        )

    def setUp(self):
        self.controls, self.ticks, self.effects = {}, {}, {}
        self.commands, self.messages = (
            {r: [] for r in ("prefill", "decode")},
            {r: [] for r in ("prefill", "decode")},
        )
        layout = PoolDescriptor(
            78, 16, 8192, 128, 1, 576, "bfloat16", 11 * 2**30, 2**30, 11 * 2**30
        )
        pairs = []
        for rank in range(16):
            p = MempoolPDControl(
                PoolPeer(f"p-{rank}", "prefill", rank, 16, 1, 7, layout),
                router_owned=True,
            )
            d = MempoolPDControl(
                PoolPeer(f"d-{rank}", "decode", rank, 16, 1, 7, layout),
                router_owned=True,
            )
            d.apply(p.apply(d.begin_handshake("tcp://d:1")))
            pairs.append((p, d))
        for index, role in enumerate(("prefill", "decode")):
            collective = CPUCollective()
            controls = [pair[index] for pair in pairs]
            self.controls[role] = controls
            self.effects[role] = [[] for _ in range(16)]
            self.ticks[role] = [
                MempoolSubrouter(
                    RouterShard(
                        c,
                        native=NativeReservations(Rows(), Pages())
                        if role == "decode"
                        else None,
                        effect=lambda kind, room, r=rank, side=role: self.effects[side][
                            r
                        ].append(kind),
                        reply_to="tcp://d:1",
                        page_size=128,
                    ),
                    run_id="cpu",
                    gather=lambda value, r=rank: collective.gather(r, value),
                    receive=lambda side=role: self.take_commands(side),
                    send=lambda msg, side=role: self.messages[side].append(msg),
                    registration=lambda c=c: dict(rank=c.local.tp_rank),
                    clock=lambda: 1.0,
                )
                for rank, c in enumerate(controls)
            ]
        self.request = dict(
            rid="r1",
            room=1,
            attempt="attempt",
            input_ids=[7] * 128,
            page_size=128,
            prompt_tokens=128,
            max_new_tokens=4,
            ignore_eos=False,
            hit_len=0,
        )
        self.op = 0

    def take_commands(self, role):
        commands, self.commands[role] = self.commands[role], []
        return commands

    def advance(self, role, facts=None):
        with ThreadPoolExecutor(max_workers=16) as workers:
            list(
                workers.map(
                    lambda rank: self.ticks[role][rank].advance(
                        [] if facts is None else facts[rank]
                    ),
                    range(16),
                )
            )

    def command(self, role, kind, **extra):
        self.op += 1
        command = dict(
            run_id="cpu",
            role=role,
            kind=kind,
            rid="r1",
            room=1,
            attempt="attempt",
            op_id=self.op,
            **extra,
        )
        self.commands[role].append(command)
        self.advance(role)
        return next(
            m for m in reversed(self.messages[role]) if m["kind"] == kind + "_ack"
        )

    def test_backend_request_cannot_change_dispatch_sampling(self):
        d = self.command("decode", "alloc", request=self.request)
        p = self.command("prefill", "alloc", request=self.request, bindings=d["ranks"])
        self.command("decode", "dispatch", request=self.request, bindings=p["ranks"])
        shard = self.ticks["decode"][0].shard
        req = SimpleNamespace(
            rid="r1",
            bootstrap_room=1,
            bootstrap_host="p",
            origin_input_ids=array("q", [7] * 128),
            sampling_params=SimpleNamespace(
                max_new_tokens=4, top_k=1, ignore_eos=False, repetition_penalty=2
            ),
        )
        with self.assertRaisesRegex(ValueError, "sampling"):
            shard.track(req)
        self.assertIsNone(shard.req)
        req.sampling_params.repetition_penalty = 1
        shard.track(req)
        self.assertIs(shard.req, req)

    def test_whole_slot_descriptors_use_unified_pages(self):
        d = self.command("decode", "alloc", request=self.request)
        p = self.command("prefill", "alloc", request=self.request, bindings=d["ranks"])
        for result, count in ((p, 64), (d, 1)):
            for rank in result["ranks"]:
                self.assertEqual(len(rank["pages"]), count)
                self.assertEqual(
                    [p["logical_page"] for p in rank["pages"]], list(range(count))
                )
                self.assertTrue(all(p["page_size"] == 128 for p in rank["pages"]))

    def test_router_page_mismatch_is_rejected_before_native_allocation(self):
        self.request["page_size"] = 64
        with self.assertRaisesRegex(RuntimeError, "page_size differ"):
            self.command("decode", "alloc", request=self.request)
        self.assertTrue(
            all(len(c.available_slots()) == 16 for c in self.controls["decode"])
        )
        self.assertTrue(
            all(not t.shard.native.reservations for t in self.ticks["decode"])
        )

    def test_duplicate_alloc_is_idempotent_and_error_retains_resources(self):
        original = self.command("decode", "alloc", request=self.request)
        command = dict(original, kind="alloc")
        command.pop("ranks")
        self.commands["decode"].append(command)
        self.advance("decode")
        self.assertEqual(
            [m for m in self.messages["decode"] if m["kind"] == "alloc_ack"],
            [original, original],
        )
        command["request"] = dict(self.request, max_new_tokens=8)
        self.commands["decode"].append(command)
        with self.assertRaisesRegex(RuntimeError, "conflicting duplicate"):
            self.advance("decode")
        self.assertTrue(
            all(len(c.available_slots()) == 15 for c in self.controls["decode"])
        )
        for tick in self.ticks["decode"]:
            self.assertEqual(len(tick.shard.native.reservations), 1)
            self.assertIsNotNone(tick.fault)

    def test_late_native_transfer_holds_first_token_and_explicit_free(self):
        d = self.command("decode", "alloc", request=self.request)
        p = self.command("prefill", "alloc", request=self.request, bindings=d["ranks"])
        d = self.command(
            "decode", "dispatch", request=self.request, bindings=p["ranks"]
        )
        self.command("prefill", "dispatch", request=self.request, bindings=d["ranks"])
        p_facts = [
            [
                RequestObservation(
                    1,
                    128,
                    4,
                    100,
                    attempt="attempt",
                    prompt_ready=True,
                    write_range=(0, 128),
                    writes_done=True,
                )
            ]
            for _ in range(16)
        ]
        self.advance("prefill", p_facts)  # BOUND -> PREFILLING
        self.advance("prefill", p_facts)  # all actual writes -> ready_tokens
        ready = next(m for m in self.messages["prefill"] if m["kind"] == "ready_tokens")
        self.command("decode", "ready", bindings=ready["ranks"])
        d_facts = [
            [
                RequestObservation(
                    1, 128, 4, 100, attempt="attempt", transfer_ready=r != 15
                )
            ]
            for r in range(16)
        ]
        self.advance("decode", d_facts)
        self.assertTrue(
            all(
                c.get_room_request(1).phase == "WAITING_READY"
                for c in self.controls["decode"]
            )
        )
        d_facts[15] = d_facts[0]
        self.advance("decode", d_facts)
        self.assertTrue(
            all(
                c.get_room_request(1).phase == "DECODING"
                for c in self.controls["decode"]
            )
        )
        self.assertTrue(
            all(
                len(c.available_slots()) == 15
                for c in self.controls["prefill"] + self.controls["decode"]
            )
        )
