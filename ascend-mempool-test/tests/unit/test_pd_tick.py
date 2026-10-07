"""Exercise the complete TP tick with real control and a CPU collective boundary."""

import pickle
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_protocol import PoolDescriptor, PoolPeer
from ascend_mempool_pd.mempool_tick import MempoolTPTick, RequestObservation


class CPUCollective:
    """Provide blocking all-gather semantics for sixteen independent workers."""

    def __init__(self, size=16):
        """Keep each collective's inputs until every worker has read them."""
        self.size = size
        self.barrier = threading.Barrier(size, timeout=10)
        self.values = [None] * size
        self.rounds = [0] * size
        self.observation_bytes = [0] * size

    def gather(self, rank, value):
        """Gather one serializable value in the same order on every rank."""
        self.values[rank] = value
        if value is not None and not isinstance(value, str):
            self.observation_bytes[rank] = len(pickle.dumps(value))
        self.barrier.wait()
        result = tuple(self.values)
        self.barrier.wait()
        self.rounds[rank] += 1
        return result


class TestMempoolTPTick(unittest.TestCase):
    """Check TP ownership at completed public advance boundaries."""

    def setUp(self):
        """Build sixteen independent pairs, sharing only same-side observations."""
        layout = PoolDescriptor(
            2, 16, 8192, 8192, 1, 576, "bfloat16", 2**30, 2**30, 2**30
        )
        self.p = []
        self.d = []
        self.pc, self.dc = CPUCollective(), CPUCollective()
        self.effects = [[] for _ in range(16)]
        for rank in range(16):
            p = MempoolPDControl(
                PoolPeer(f"p-{rank}", "prefill", rank, 16, 1, 7, layout)
            )
            d = MempoolPDControl(
                PoolPeer(f"d-{rank}", "decode", rank, 16, 1, 7, layout)
            )
            d.apply(p.apply(d.begin_handshake(f"tcp://d:{rank}")))
            self.p.append(p)
            self.d.append(d)
        self.pt = self.ticks(self.p, self.d, self.pc)
        self.dt = self.ticks(self.d, self.p, self.dc)

    def ticks(self, controls, peers, collective):
        """Connect transport and collectives at their external boundaries."""
        return [
            MempoolTPTick(
                control,
                gather=lambda value, r=rank: collective.gather(r, value),
                send=lambda endpoint, message, r=rank: peers[r].enqueue(message),
                reply_to=f"tcp://local:{rank}",
                endpoint=f"tcp://peer:{rank}",
                effect=lambda kind, room, r=rank: self.effects[r].append((kind, room)),
                clock=lambda: 1.0,
            )
            for rank, control in enumerate(controls)
        ]

    def advance(self, ticks, facts=()):
        """Run one tick on every rank and propagate worker failures."""
        with ThreadPoolExecutor(max_workers=16) as workers:
            list(workers.map(lambda tick: tick.advance(facts), ticks))

    def test_empty_ticks_keep_fixed_collective_sequence(self):
        """Empty input uses the same prepare/commit sequence as active ticks."""
        self.advance(self.pt)
        self.advance(self.pt)
        self.assertEqual(self.pc.rounds, [4] * 16)
        self.assertTrue(all(len(c.available_slots()) == 16 for c in self.p))

    def test_decode_acquire_is_same_slot_and_attempt_on_all_ranks(self):
        """Leader attempt and independent D slot are agreed before ACQUIRE leaves."""
        with ThreadPoolExecutor(max_workers=16) as workers:
            list(
                workers.map(
                    lambda rank: self.dt[rank].advance(
                        (RequestObservation(42, 32, 8, 100, attempt=f"attempt-{rank}"),)
                    ),
                    range(16),
                )
            )
        records = [c.snapshot().requests[0] for c in self.d]
        self.assertEqual({r.identity.attempt for r in records}, {"attempt-0"})
        self.assertEqual({r.d_slot.slot for r in records}, {0})
        self.assertTrue(all(len(c.available_slots()) == 15 for c in self.d))
        self.assertTrue(all(len(c.available_slots()) == 16 for c in self.p))
        self.assertEqual(self.dc.rounds, [2] * 16)

    def test_tick_payload_does_not_grow_with_unrelated_terminal_history(self):
        """One active request sends the same observations after 256 completed ones."""
        requests = [
            d.acquire_decode(999, "active", 1, 32, 8, "tcp://d:1").request
            for d in self.d
        ]
        self.advance(self.dt)
        before = self.dc.observation_bytes.copy()
        for d, request in zip(self.d, requests):
            for room in range(256):
                old = d.acquire_decode(room, str(room), 0, 32, 8, "tcp://d:1")
                d.cancel_local(old.request, "finished")
            self.assertEqual(len(d.snapshot().requests), 257)
            self.assertEqual(d.get_request(request).phase, "ACQUIRED")
        self.advance(self.dt)
        self.assertEqual(self.dc.observation_bytes, before)
        self.assertTrue(all(d.has_pending_requests() for d in self.d))

    def test_handoff_keeps_p_slot_until_uniform_drain_and_ack(self):
        """Sixteen pairs bind, wait for both readiness facts, then retire together."""
        from msgspec.structs import replace

        facts = (RequestObservation(42, 32, 8, 100, attempt="attempt-42"),)
        self.advance(self.dt, facts)
        for _ in range(4):
            self.advance(self.pt, facts)
            self.advance(self.dt, facts)
        self.assertTrue(
            all(c.snapshot().requests[0].phase == "PREFILLING" for c in self.p)
        )
        transferred = (replace(facts[0], transfer_ready=True),)
        self.advance(self.dt, transferred)
        self.assertTrue(
            all(c.snapshot().requests[0].phase == "WAITING_READY" for c in self.d)
        )
        written = (
            replace(facts[0], prompt_ready=True, writes_done=True, native_release=True),
        )
        self.advance(self.pt, written)
        for _ in range(3):
            self.advance(self.dt, transferred)
        self.assertTrue(
            all(c.snapshot().requests[0].phase == "DECODING" for c in self.d)
        )
        self.assertTrue(all(len(c.available_slots()) == 15 for c in self.p))
        finished = (replace(transferred[0], release=True),)
        self.advance(self.dt, finished)
        self.assertTrue(all(len(c.available_slots()) == 15 for c in self.d))
        drained = (replace(finished[0], drained=True),)
        self.advance(self.dt, drained)
        self.assertTrue(all(len(c.available_slots()) == 16 for c in self.d))
        self.advance(self.pt, (replace(written[0], native_freed=True),))
        self.advance(self.dt, drained)
        self.assertTrue(all(len(c.available_slots()) == 16 for c in self.p))
        self.assertTrue(all(c.snapshot().requests[0].phase == "CLOSED" for c in self.d))

    def test_one_late_rank_holds_acquire_observations_until_all_arrive(self):
        """Earlier arrivals survive empty ticks; no P rank acquires alone."""
        facts = (RequestObservation(91, 32, 8, 100, attempt="late"),)
        self.advance(self.dt, facts)
        late = self.p[-1].drain_inbox()
        self.advance(self.pt, facts)
        self.advance(self.pt, facts)
        self.assertTrue(all(not c.snapshot().requests for c in self.p))
        for message in late:
            self.p[-1].enqueue(message)
        self.advance(self.pt, facts)
        self.advance(self.pt, facts)
        self.assertEqual({c.snapshot().requests[0].p_slot.slot for c in self.p}, {0})

    def test_terminal_service_fact_can_retire_on_one_rank_before_the_others(self):
        """Completed native queues need not drop their last Req on the same tick."""
        for d in self.d:
            request = d.acquire_decode(42, "old", 0, 32, 8, "tcp://d:1").request
            d.cancel_local(request, "finished")
        fact = RequestObservation(
            42,
            32,
            8,
            100,
            attempt="old",
            cancel=True,
            release=True,
            drained=True,
            host_drained=True,
            native_freed=True,
        )
        for remaining in (16, 15, 1):
            with ThreadPoolExecutor(max_workers=16) as workers:
                list(
                    workers.map(
                        lambda rank: self.dt[rank].advance(
                            (fact,) if rank < remaining else ()
                        ),
                        range(16),
                    )
                )
        self.assertTrue(all(len(d.available_slots()) == 16 for d in self.d))
        self.assertFalse(any(d.has_pending_requests() for d in self.d))
        self.assertEqual(self.effects, [[] for _ in range(16)])

    def test_evicted_terminal_fact_cannot_admit_an_already_freed_request(self):
        """Old native queue entries do not acquire storage after history eviction."""
        for rank, p in enumerate(self.p):
            d = MempoolPDControl(self.d[rank].local, max_records=1)
            d.apply(p.apply(d.begin_handshake("tcp://d:1")))
            old = d.acquire_decode(42, "old", 0, 32, 8, "tcp://d:1").request
            d.cancel_local(old, "finished")
            next_request = d.acquire_decode(43, "next", 0, 32, 8, "tcp://d:1").request
            d.cancel_local(next_request, "finished")
            self.assertIsNone(d.get_request(old))
            self.d[rank] = d
        self.dt = self.ticks(self.d, self.p, self.dc)
        self.advance(
            self.dt,
            (
                RequestObservation(
                    42,
                    32,
                    8,
                    100,
                    attempt="old",
                    release=True,
                    native_freed=True,
                ),
            ),
        )
        self.assertTrue(all(len(d.available_slots()) == 16 for d in self.d))
        self.assertFalse(any(d.has_pending_requests() for d in self.d))
        self.assertEqual(self.effects, [[] for _ in range(16)])

    def test_late_retirement_after_reuse_allows_different_evicted_history(self):
        """Each rank verifies old proofs locally even if only some retain the record."""
        old_done, old_ack, current = [], [], []
        for rank in range(16):
            p = MempoolPDControl(self.p[rank].local, max_records=1 if rank == 0 else 2)
            d = MempoolPDControl(self.d[rank].local, max_records=1 if rank == 0 else 2)
            d.apply(p.apply(d.begin_handshake("tcp://d:1")))
            old = d.acquire_decode(42, "old", 0, 32, 8, "tcp://d:1").request
            # Establish the same old binding via the normal protocol transitions.
            p.apply(d.acquire_decode(42, "old", 0, 32, 8, "tcp://d:1"))
            p.apply(d.apply(p.acquire_prefill(old, 0)))
            d.cancel_local(old, "finished")
            d.begin_drain(old)
            done = d.finish_drain(old)
            ack = p.apply(done)
            d.apply(ack)
            acquire = d.acquire_decode(42, "new", 0, 32, 8, "tcp://d:1")
            p.apply(acquire)
            p.apply(d.apply(p.acquire_prefill(acquire.request, 0)))
            self.assertEqual(p.get_request(old) is None, rank == 0)
            old_done.append(done)
            old_ack.append(ack)
            current.append(acquire.request)
            self.p[rank], self.d[rank] = p, d
        self.pt = self.ticks(self.p, self.d, self.pc)
        self.dt = self.ticks(self.d, self.p, self.dc)
        before = [c.get_request(r) for c, r in zip(self.p + self.d, current * 2)]
        for _ in range(2):
            for p, d, done, ack in zip(self.p, self.d, old_done, old_ack):
                p.enqueue(done)
                d.enqueue(ack)
            self.advance(self.pt)
            self.advance(self.dt)
            after = [c.get_request(r) for c, r in zip(self.p + self.d, current * 2)]
            self.assertEqual(after, before)
            self.assertTrue(
                all(
                    c.available_slots() == frozenset(range(1, 16))
                    for c in self.p + self.d
                )
            )
        self.assertEqual(self.effects, [[] for _ in range(16)])

    def test_capacity_wait_uses_original_deadline_without_allocating(self):
        """A seventeenth request waits, then retires without taking a busy slot."""
        from msgspec.structs import replace

        for control in self.d:
            for slot in range(16):
                control.acquire_decode(slot, str(slot), slot, 32, 8, "tcp://d:1")
        waiting = RequestObservation(99, 32, 8, 100, attempt="waiting")
        self.advance(self.dt, (waiting,))
        self.assertTrue(all(not c.available_slots() for c in self.d))
        self.assertEqual(self.effects, [[] for _ in range(16)])
        self.advance(self.dt, (replace(waiting, deadline=0.5),))
        self.assertTrue(all(len(c.snapshot().requests) == 16 for c in self.d))
        self.assertTrue(
            all(
                e == [("reject", 99), ("drain", 99), ("native_release", 99)]
                for e in self.effects
            )
        )

    def test_batch_admission_waits_for_record_budget_on_every_rank(self):
        """Individually valid candidates cannot overspend the shared record budget."""
        from msgspec.structs import replace

        for rank, p in enumerate(self.p):
            d = MempoolPDControl(self.d[rank].local, max_records=1)
            d.apply(p.apply(d.begin_handshake("tcp://d:1")))
            self.d[rank] = d
        self.dt = self.ticks(self.d, self.p, self.dc)
        first = RequestObservation(42, 32, 8, 100, attempt="first")
        second = RequestObservation(43, 32, 8, 100, attempt="second")
        self.advance(self.dt, (first, second))
        for d in self.d:
            self.assertEqual(d.get_room_request(42).d_slot.slot, 0)
            self.assertIsNone(d.get_room_request(43))
            self.assertEqual(len(d.available_slots()), 15)
        self.advance(self.dt, (replace(first, cancel=True), second))
        self.assertTrue(all(d.get_room_request(43) is None for d in self.d))
        self.advance(self.dt, (second,))
        for d in self.d:
            current = d.get_room_request(43)
            self.assertEqual((current.d_slot.slot, current.d_slot.generation), (0, 2))
            self.assertFalse(d.has_retained_room(42))
        self.assertEqual(self.dc.rounds, [6] * 16)

    def test_one_rank_cancellation_is_prepared_on_every_rank(self):
        """A rank without a local cancel flag still prepares the common rollback."""
        from msgspec.structs import replace

        fact = RequestObservation(42, 32, 8, 100, attempt="cancel")
        self.advance(self.dt, (fact,))
        with ThreadPoolExecutor(max_workers=16) as workers:
            list(
                workers.map(
                    lambda rank: self.dt[rank].advance(
                        (replace(fact, cancel=rank == 15),)
                    ),
                    range(16),
                )
            )
        self.assertTrue(all(not d.has_pending_requests() for d in self.d))
        self.assertTrue(all(len(d.available_slots()) == 16 for d in self.d))
        self.assertEqual(self.effects, [[("cancel", 42), ("drain", 42)]] * 16)
        self.assertEqual(self.dc.rounds, [4] * 16)

    def test_cancel_on_one_rank_prevents_fresh_admission(self):
        """A pending request cancelled on a non-leader never acquires a slot."""
        with ThreadPoolExecutor(max_workers=16) as workers:
            list(
                workers.map(
                    lambda rank: self.dt[rank].advance(
                        (
                            RequestObservation(
                                42,
                                32,
                                8,
                                100,
                                attempt=f"local-{rank}",
                                cancel=rank == 15,
                            ),
                        )
                    ),
                    range(16),
                )
            )
        self.assertTrue(all(not d.has_pending_requests() for d in self.d))
        self.assertTrue(all(len(d.available_slots()) == 16 for d in self.d))
        self.assertEqual(
            self.effects, [[("reject", 42), ("drain", 42), ("native_release", 42)]] * 16
        )
        self.assertEqual(self.dc.rounds, [2] * 16)

    def test_commit_effect_failure_prevents_all_outbox_sends(self):
        """A runtime failure after preparation is fatal, even if peers committed."""
        from msgspec.structs import replace

        fact = RequestObservation(42, 32, 8, 100, attempt="effect-failure")
        self.advance(self.dt, (fact,))
        for p in self.p:
            p.drain_inbox()

        def fail_effect(kind, room):
            raise RuntimeError("injected native effect failure")

        self.dt[-1].effect = fail_effect
        with self.assertRaisesRegex(RuntimeError, "injected native effect failure"):
            self.advance(self.dt, (replace(fact, cancel=True),))
        self.assertEqual([p.drain_inbox() for p in self.p], [[]] * 16)
        for d in self.d:
            with self.assertRaisesRegex(RuntimeError, "injected native effect failure"):
                d.assert_healthy()
        self.assertEqual(self.dc.rounds, [4] * 16)

    def test_valid_proof_cannot_skip_the_binding_phase(self):
        """Preparation checks the transition, not only the signed allocation."""
        from ascend_mempool_pd.mempool_protocol import MempoolMessage, MessageType

        for p, d in zip(self.p, self.d):
            acquire = d.acquire_decode(42, "early-done", 0, 32, 8, "tcp://d:1")
            p.apply(acquire)
            acquired = p.acquire_prefill(acquire.request, 0)
            p.enqueue(
                MempoolMessage(
                    MessageType.DONE,
                    request=acquired.request,
                    p_slot=acquired.p_slot,
                    d_slot=acquired.d_slot,
                    reason="completed",
                )
            )
        before = [p.snapshot() for p in self.p]
        with self.assertRaisesRegex(RuntimeError, "DONE arrived outside a bound"):
            self.advance(self.pt)
        for old, p in zip(before, self.p):
            self.assertEqual(p.snapshot().requests, old.requests)
            self.assertEqual(p.available_slots(), old.available_slots)
        self.assertEqual([d.drain_inbox() for d in self.d], [[]] * 16)
        self.assertEqual(self.pc.rounds, [2] * 16)

    def test_one_failed_preparation_prevents_every_transition_and_outbox(self):
        """A malformed pair-local proof cannot partially advance same-side ownership."""
        from msgspec.structs import replace

        from ascend_mempool_pd.mempool_protocol import MempoolMessage, MessageType

        facts = (RequestObservation(52, 32, 8, 100, attempt="invalid"),)
        self.advance(self.dt, facts)
        for _ in range(3):
            self.advance(self.pt, facts)
            self.advance(self.dt, facts)
        # A valid DONE sorts before the bad one. Neither may change ownership.
        for p, d in zip(self.p, self.d):
            acquire = d.acquire_decode(51, "valid", 1, 32, 8, "tcp://d:1")
            p.apply(acquire)
            p.apply(d.apply(p.acquire_prefill(acquire.request, 1)))
        before = [c.snapshot() for c in self.p]
        for rank, p in enumerate(self.p):
            p.drain_inbox()
            for record in p.snapshot().requests:
                p.enqueue(
                    MempoolMessage(
                        MessageType.DONE,
                        request=record.identity,
                        p_slot=replace(record.p_slot, proof="0" * 64)
                        if rank == 15 and record.identity.room == 52
                        else record.p_slot,
                        d_slot=record.d_slot,
                        reason="test",
                    )
                )
        safe = (
            replace(facts[0], native_freed=True),
            replace(facts[0], room=51, attempt="valid", native_freed=True),
        )
        rounds = self.pc.rounds[0]
        with self.assertRaisesRegex(RuntimeError, "mempool TP tick failed"):
            self.advance(self.pt, safe)
        for old, control in zip(before, self.p):
            self.assertEqual(control.snapshot().available_slots, old.available_slots)
            self.assertEqual(control.snapshot().requests, old.requests)
        self.assertEqual([c.drain_inbox() for c in self.d], [[]] * 16)
        self.assertEqual(self.pc.rounds, [rounds + 2] * 16)
