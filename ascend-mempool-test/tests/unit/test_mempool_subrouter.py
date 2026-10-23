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
