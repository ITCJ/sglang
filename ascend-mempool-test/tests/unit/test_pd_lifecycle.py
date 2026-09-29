"""Exercise request ownership through the Ascend mempool control API."""

import unittest
from dataclasses import replace

from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_protocol import (
    MempoolMessage,
    MessageType,
    PoolDescriptor,
    PoolPeer,
    RequestIdentity,
    SlotLease,
)


class TestMempoolPDControl(unittest.TestCase):
    """Observe slot reuse and readiness across one paired P/D control path."""

    def setUp(self):
        """Construct a mapped-compatible pair with independent slot allocators."""
        layout = PoolDescriptor(
            layers=2,
            slots=16,
            prompt_tokens=8192,
            decode_tokens=8192,
            heads=1,
            dim=576,
            dtype="bfloat16",
            prompt_bytes=1073741824,
            decode_bytes=1073741824,
            stride_bytes=1073741824,
        )
        self.p = MempoolPDControl(PoolPeer("p-boot", "prefill", 0, 16, 1, 7, layout))
        self.d = MempoolPDControl(PoolPeer("d-boot", "decode", 0, 16, 1, 7, layout))
        ready = self.p.apply(self.d.begin_handshake("tcp://d:4351"))
        self.assertEqual(ready.kind, MessageType.POOL_READY)
        self.d.apply(ready)

    def test_request_keeps_prompt_slot_until_decode_done(self):
        """Handoff readiness and decode completion have distinct release points."""
        acquire = self.d.acquire_decode(
            room=27,
            attempt="attempt-a",
            slot=5,
            prompt_tokens=2048,
            decode_tokens=512,
            reply_to="tcp://d:4351",
        )
        request = acquire.request
        self.assertEqual(acquire.kind, MessageType.ACQUIRE)
        self.assertIsNone(self.p.apply(acquire))
        acquired = self.p.acquire_prefill(request, slot=7)
        bound_ack = self.d.apply(acquired)
        self.p.apply(bound_ack)
        self.p.start_prefill(request)

        self.d.transfer_succeeded(request)
        self.assertFalse(self.d.can_decode(request))
        self.p.finish_prefill_writes(request)
        kv_ready = self.p.publish_kv_ready(request, prompt_tokens=2048)
        self.assertEqual(kv_ready.kind, MessageType.KV_READY)
        self.d.apply(kv_ready)
        self.assertTrue(self.d.can_decode(request))
        self.d.start_decode(request)
        self.assertNotIn(5, self.d.available_slots())
        self.assertNotIn(7, self.p.available_slots())

        self.d.begin_drain(request)
        self.assertNotIn(5, self.d.available_slots())
        done = self.d.finish_drain(request)
        self.assertIn(5, self.d.available_slots())
        self.assertNotIn(7, self.p.available_slots())
        release_ack = self.p.apply(done)
        self.assertEqual(release_ack.kind, MessageType.RELEASE_ACK)
        self.assertIn(7, self.p.available_slots())
        self.d.apply(release_ack)
        self.assertEqual(self.d.state(request), "CLOSED")

    def test_duplicate_cancel_preserves_drain_progress(self):
        """Repeated remote and local cancellation cannot undo an ongoing drain."""
        acquire = self.d.acquire_decode(27, "drain", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, 7)))
        cancel = self.p.cancel_local(acquire.request, "prefill aborted")
        self.d.apply(cancel)
        self.d.begin_drain(acquire.request)
        self.d.apply(cancel)
        self.d.cancel_local(acquire.request, "client disconnected")
        self.assertEqual(self.d.state(acquire.request), "DRAINING")
        self.assertNotIn(5, self.d.available_slots())
        done = self.d.finish_drain(acquire.request)
        self.d.apply(cancel)
        self.assertEqual(self.d.state(acquire.request), "WAITING_RELEASE_ACK")
        self.d.apply(self.p.apply(done))
        self.d.apply(cancel)
        self.assertEqual(self.d.state(acquire.request), "CLOSED")

    def test_late_ready_cannot_revive_cancelled_or_closed_binding(self):
        """A ready/cancel race stays harmless throughout drain and slot reuse."""
        acquire = self.d.acquire_decode(27, "late-ready", 5, 32, 16, "tcp://d:4351")
        request = acquire.request
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(request, 7)))
        self.p.start_prefill(request)
        self.p.finish_prefill_writes(request)
        ready = self.p.publish_kv_ready(request, 32)
        self.d.cancel_local(request, "client disconnected")
        for phase in ("CANCELLING", "DRAINING", "WAITING_RELEASE_ACK", "CLOSED"):
            with self.subTest(phase=phase):
                self.assertIsNone(self.d.apply(ready))
                self.assertEqual(self.d.state(request), phase)
                self.assertFalse(self.d.can_decode(request))
                conflicting = replace(
                    ready, p_slot=replace(ready.p_slot, generation=99)
                )
                with self.assertRaisesRegex(ValueError, "stale slot"):
                    self.d.apply(conflicting)
            if phase == "CANCELLING":
                self.d.begin_drain(request)
            elif phase == "DRAINING":
                done = self.d.finish_drain(request)
            elif phase == "WAITING_RELEASE_ACK":
                self.d.apply(self.p.apply(done))
        second = self.d.acquire_decode(27, "new-owner", 5, 32, 16, "tcp://d:4351")
        self.d.apply(ready)
        self.assertEqual(self.d.state(second.request), "ACQUIRED")
        self.assertNotIn(5, self.d.available_slots())

    def test_invalid_acquire_leaves_slot_and_room_available(self):
        """Rejected local metadata must not acquire or consume a generation."""
        for override in (
            {"prompt_tokens": 1.5},
            {"decode_tokens": True},
            {"prompt_tokens": "32"},
            {"reply_to": ""},
            {"reply_to": "   "},
            {"slot": True},
        ):
            with self.subTest(override=override):
                d = MempoolPDControl(self.d.local)
                d.apply(self.p.apply(d.begin_handshake("tcp://d:4351")))
                args = dict(
                    room=27,
                    attempt="invalid",
                    slot=5,
                    prompt_tokens=32,
                    decode_tokens=16,
                    reply_to="tcp://d:4351",
                )
                with self.assertRaises(ValueError):
                    d.acquire_decode(**(args | override))
                self.assertEqual(len(d.available_slots()), 16)
                valid = d.acquire_decode(**(args | {"attempt": "valid"}))
                self.assertEqual(valid.d_slot.generation, 1)

    def test_replayed_done_cannot_release_new_owner_of_same_slot(self):
        """An old request may receive its old ACK without freeing a new lease."""
        first = self.d.acquire_decode(27, "attempt-a", 5, 32, 16, "tcp://d:4351")
        self.p.apply(first)
        acquired = self.p.acquire_prefill(first.request, 7)
        self.p.apply(self.d.apply(acquired))
        self.p.start_prefill(first.request)
        self.p.finish_prefill_writes(first.request)
        self.d.apply(self.p.publish_kv_ready(first.request, 32))
        self.d.transfer_succeeded(first.request)
        self.d.start_decode(first.request)
        self.d.begin_drain(first.request)
        done = self.d.finish_drain(first.request)
        release_ack = self.p.apply(done)
        self.d.apply(release_ack)

        second = self.d.acquire_decode(27, "attempt-b", 5, 32, 16, "tcp://d:4351")
        self.p.apply(second)
        acquired = self.p.acquire_prefill(second.request, 7)
        self.assertNotEqual(first.d_slot.generation, second.d_slot.generation)
        self.assertNotEqual(done.p_slot.generation, acquired.p_slot.generation)
        self.assertEqual(self.p.apply(done), release_ack)
        self.assertNotIn(7, self.p.available_slots())
        self.assertEqual(self.p.state(second.request), "ACQUIRED")

    def test_cancel_before_acquire_blocks_late_allocation(self):
        """A late ACQUIRE cannot revive an attempt already cancelled by D."""
        request = RequestIdentity(27, "aborted", "p-boot", "d-boot")
        cancel = MempoolMessage(
            MessageType.CANCEL,
            request=request,
            d_slot=SlotLease(5, 1),
            reason="client disconnected",
        )
        self.p.apply(cancel)
        late_acquire = MempoolMessage(
            MessageType.ACQUIRE,
            request=request,
            d_slot=SlotLease(5, 1),
            reply_to="tcp://d:4351",
            prompt_tokens=32,
            decode_tokens=16,
        )
        self.assertIsNone(self.p.apply(late_acquire))
        self.assertIn(7, self.p.available_slots())
        with self.assertRaises(ValueError):
            self.p.acquire_prefill(request, 7)

    def test_inbox_only_changes_state_when_scheduler_applies_messages(self):
        """A ZMQ reader may enqueue ACQUIRE but cannot reserve P storage itself."""
        acquire = self.d.acquire_decode(27, "queued", 5, 32, 16, "tcp://d:4351")
        self.p.enqueue(acquire)
        self.assertEqual(len(self.p.available_slots()), 16)
        self.assertEqual(self.p.drain_inbox(), [acquire])
        with self.assertRaises(ValueError):
            self.p.state(acquire.request)
        self.p.apply(acquire)
        self.assertEqual(self.p.state(acquire.request), "WAITING_ACQUIRE")
        self.assertEqual(self.p.drain_inbox(), [])

    def test_unbound_cancel_rolls_back_acquired_slot(self):
        """D can reject a P slot it has not bound, then both sides release."""
        acquire = self.d.acquire_decode(27, "rollback", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        self.p.acquire_prefill(acquire.request, 7)
        unbound_cancel = self.d.cancel_local(
            acquire.request, "another D rank has no slot"
        )
        self.p.apply(unbound_cancel)
        self.assertIn(7, self.p.available_slots())
        self.assertIn(5, self.d.available_slots())
        self.assertEqual(self.p.state(acquire.request), "CANCELLED")
        self.assertIsNone(self.p.apply(acquire))

    def test_cancel_after_d_binding_before_p_bound_ack_keeps_p_slot(self):
        """P must accept DONE even if cancellation overtakes BOUND_ACK."""
        acquire = self.d.acquire_decode(27, "cancel-race", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        acquired = self.p.acquire_prefill(acquire.request, 7)
        bound_ack = self.d.apply(acquired)
        cancel = self.d.cancel_local(acquire.request, "client disconnected")
        self.p.apply(cancel)
        self.assertNotIn(7, self.p.available_slots())

        self.d.begin_drain(acquire.request)
        done = self.d.finish_drain(acquire.request, reason="cancelled")
        release_ack = self.p.apply(done)
        self.assertEqual(release_ack.kind, MessageType.RELEASE_ACK)
        self.assertIn(7, self.p.available_slots())
        self.d.apply(release_ack)
        self.assertEqual(self.d.state(acquire.request), "CLOSED")
        self.assertEqual(self.p.apply(done), release_ack)
        self.p.apply(bound_ack)

    def test_late_acquired_after_unbound_cancel_cannot_reacquire(self):
        """An in-flight P binding cannot revive D after its rollback."""
        acquire = self.d.acquire_decode(27, "late-binding", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        acquired = self.p.acquire_prefill(acquire.request, 7)
        self.p.apply(self.d.cancel_local(acquire.request, "rank rollback"))
        self.assertIn(7, self.p.available_slots())
        late_cancel = self.d.apply(acquired)
        self.assertEqual(late_cancel.kind, MessageType.CANCEL)
        self.p.apply(late_cancel)
        self.assertEqual(self.d.state(acquire.request), "CANCELLED")

    def test_p_cancel_after_acquired_waits_for_d_unbound_response(self):
        """A locally cancelled P slot stays held until D confirms no binding."""
        acquire = self.d.acquire_decode(27, "p-cancel", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        self.p.acquire_prefill(acquire.request, 7)
        p_cancel = self.p.cancel_local(acquire.request, "rank rollback")
        self.assertNotIn(7, self.p.available_slots())
        d_cancel = self.d.apply(p_cancel)
        self.assertEqual(d_cancel.kind, MessageType.CANCEL)
        self.assertIsNone(d_cancel.p_slot)
        self.p.apply(d_cancel)
        self.assertIn(7, self.p.available_slots())

    def test_p_cancel_after_binding_waits_for_drain(self):
        """A P-side abort cannot reclaim prompt KV while D knows its binding."""
        acquire = self.d.acquire_decode(27, "p-bound-cancel", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, 7)))
        p_cancel = self.p.cancel_local(acquire.request, "prefill failed")
        self.assertIsNone(self.d.apply(p_cancel))
        self.assertNotIn(7, self.p.available_slots())
        self.d.begin_drain(acquire.request)
        done = self.d.finish_drain(acquire.request, reason="prefill failed")
        self.d.apply(self.p.apply(done))
        self.assertIn(7, self.p.available_slots())

    def test_old_acquire_stays_rejected_after_terminal_record_eviction(self):
        """D generation prevents stale acquire from reviving an evicted attempt."""
        p = MempoolPDControl(self.p.local, max_records=1)
        p.apply(self.d.begin_handshake("tcp://d:4351"))
        first = self.d.acquire_decode(30, "first", 5, 32, 16, "tcp://d:4351")
        p.apply(first)
        p.apply(self.d.cancel_local(first.request, "rollback"))
        second = self.d.acquire_decode(31, "second", 5, 32, 16, "tcp://d:4351")
        p.apply(second)
        p.apply(self.d.cancel_local(second.request, "rollback"))

        with self.assertRaisesRegex(ValueError, "unknown"):
            p.state(first.request)
        self.assertIsNone(p.apply(first))
        self.assertEqual(len(p.available_slots()), 16)

    def test_done_replay_after_terminal_eviction_cannot_release_new_owner(self):
        """A compact generation watermark can ACK an old DONE without freeing KV."""
        p = MempoolPDControl(self.p.local, max_records=1)
        p.apply(self.d.begin_handshake("tcp://d:4351"))
        first = self.d.acquire_decode(30, "first-done", 5, 32, 16, "tcp://d:4351")
        p.apply(first)
        acquired = p.acquire_prefill(first.request, 7)
        p.apply(self.d.apply(acquired))
        p.start_prefill(first.request)
        p.finish_prefill_writes(first.request)
        self.d.apply(p.publish_kv_ready(first.request, 32))
        self.d.transfer_succeeded(first.request)
        self.d.start_decode(first.request)
        self.d.begin_drain(first.request)
        done = self.d.finish_drain(first.request)
        release_ack = p.apply(done)
        self.d.apply(release_ack)

        second = self.d.acquire_decode(31, "second-done", 5, 32, 16, "tcp://d:4351")
        p.apply(second)
        p.acquire_prefill(second.request, 7)
        self.assertNotIn(7, p.available_slots())
        self.assertEqual(p.apply(done), release_ack)
        self.assertNotIn(7, p.available_slots())
        forged = MempoolMessage(
            MessageType.DONE,
            request=RequestIdentity(99, "never-bound", "p-boot", "d-boot"),
            p_slot=done.p_slot,
            d_slot=done.d_slot,
            reason="forged replay",
        )
        with self.assertRaisesRegex(ValueError, "binding proof"):
            p.apply(forged)

    def test_rollback_ack_is_consistent_before_and_after_record_eviction(self):
        """A retired allocation can be confirmed without touching a new owner."""
        p = MempoolPDControl(self.p.local, max_records=1)
        p.apply(self.d.begin_handshake("tcp://d:4351"))
        old = self.d.acquire_decode(30, "unbound", 5, 32, 16, "tcp://d:4351")
        p.apply(old)
        binding = p.acquire_prefill(old.request, 7)
        done = MempoolMessage(
            MessageType.DONE,
            request=old.request,
            p_slot=binding.p_slot,
            d_slot=old.d_slot,
            reason="late confirmation",
        )
        # Issuing an allocation proof alone does not mean it has retired.
        with self.assertRaises(ValueError):
            p.apply(done)
        self.assertNotIn(7, p.available_slots())
        self.assertIsNone(p.apply(self.d.cancel_local(old.request, "rollback")))
        ack = p.apply(done)
        self.assertEqual(ack.kind, MessageType.RELEASE_ACK)
        self.assertEqual(p.state(old.request), "CANCELLED")
        self.assertIn(7, p.available_slots())
        late_bound = MempoolMessage(
            MessageType.BOUND_ACK,
            request=old.request,
            p_slot=binding.p_slot,
            d_slot=old.d_slot,
        )
        p.apply(late_bound)
        self.assertEqual(p.state(old.request), "CANCELLED")

        new = self.d.acquire_decode(30, "new-owner", 5, 32, 16, "tcp://d:4351")
        p.apply(new)
        p.acquire_prefill(new.request, 7)
        with self.assertRaisesRegex(ValueError, "unknown"):
            p.state(old.request)
        self.assertEqual(p.apply(done), ack)
        p.apply(late_bound)
        for forged in (
            replace(done, request=replace(old.request, attempt="never-issued")),
            replace(done, request=replace(old.request, p_session="old-boot")),
            replace(done, p_slot=replace(binding.p_slot, generation=2)),
            replace(done, p_slot=replace(binding.p_slot, slot=8)),
            replace(done, d_slot=replace(old.d_slot, proof="0" * 64)),
        ):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                p.apply(forged)
        self.assertNotIn(7, p.available_slots())
        self.assertEqual(p.state(new.request), "ACQUIRED")

    def test_release_history_has_no_cumulative_request_limit(self):
        """Pass the old 65,536-release limit with bounded terminal records."""
        p = MempoolPDControl(self.p.local, max_records=1)
        d = MempoolPDControl(self.d.local, max_records=1)
        d.apply(p.apply(d.begin_handshake("tcp://d:4351")))
        first_done = first_ack = None
        for index in range(65537):
            acquire = d.acquire_decode(30, str(index), 5, 32, 16, "tcp://d:4351")
            request = acquire.request
            p.apply(acquire)
            p.apply(d.apply(p.acquire_prefill(request, 7)))
            d.cancel_local(request, "completed without submitted work")
            d.begin_drain(request)
            done = d.finish_drain(request)
            ack = p.apply(done)
            d.apply(ack)
            if index == 0:
                first_done, first_ack = done, ack
        self.assertEqual(p.apply(first_done), first_ack)
        self.assertEqual(len(p.available_slots()), 16)
        self.assertEqual(len(d.available_slots()), 16)
        self.assertEqual(len(p._records), 1)
        self.assertEqual(len(d._records), 1)
        self.assertEqual(len(p._retired_generation), 16)
        self.assertEqual(p._retired_generation[7], 65537)

    def test_evicted_d_terminal_ignores_verified_late_messages(self):
        """A pruned CLOSED request cannot alter a later D slot occupant."""
        d = MempoolPDControl(self.d.local, max_records=1)
        d.apply(self.p.apply(d.begin_handshake("tcp://d:4351")))
        first = d.acquire_decode(30, "d-first", 5, 32, 16, "tcp://d:4351")
        self.p.apply(first)
        acquired = self.p.acquire_prefill(first.request, 7)
        self.p.apply(d.apply(acquired))
        self.p.start_prefill(first.request)
        self.p.finish_prefill_writes(first.request)
        kv_ready = self.p.publish_kv_ready(first.request, 32)
        d.apply(kv_ready)
        d.transfer_succeeded(first.request)
        d.start_decode(first.request)
        d.begin_drain(first.request)
        release_ack = self.p.apply(d.finish_drain(first.request))
        d.apply(release_ack)
        second = d.acquire_decode(31, "d-second", 5, 32, 16, "tcp://d:4351")
        self.assertEqual(second.d_slot.generation, 2)

        self.assertIsNone(d.apply(acquired))
        self.assertIsNone(d.apply(kv_ready))
        self.assertIsNone(d.apply(release_ack))
        self.assertNotIn(5, d.available_slots())

    def test_decode_retry_rejects_conflicting_metadata(self):
        """An attempt cannot silently change its requested slot or capacity."""
        self.d.acquire_decode(27, "retry", 5, 32, 16, "tcp://d:4351")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.d.acquire_decode(27, "retry", 6, 32, 16, "tcp://d:4351")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.d.acquire_decode(27, "retry", 5, 64, 16, "tcp://d:4351")

    def test_protocol_fault_blocks_new_prefill_and_decode_work(self):
        """A receive fault stops new NPU work even after binding completed."""
        acquire = self.d.acquire_decode(27, "fault", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        acquired = self.p.acquire_prefill(acquire.request, 7)
        self.p.apply(self.d.apply(acquired))
        self.p.fail_protocol("bad tagged frame")
        with self.assertRaisesRegex(RuntimeError, "bad tagged frame"):
            self.p.start_prefill(acquire.request)

        self.d.transfer_succeeded(acquire.request)
        self.d.apply(
            MempoolMessage(
                MessageType.KV_READY,
                request=acquire.request,
                p_slot=acquired.p_slot,
                d_slot=acquire.d_slot,
                prompt_tokens=32,
            )
        )
        self.d.fail_protocol("bad tagged frame")
        with self.assertRaisesRegex(RuntimeError, "bad tagged frame"):
            self.d.start_decode(acquire.request)

    def test_cancelled_prefill_waits_for_local_writes_after_done(self):
        """D drain cannot release P while a prompt offload is still in flight."""
        acquire = self.d.acquire_decode(27, "cancelled", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        acquired = self.p.acquire_prefill(acquire.request, 7)
        self.p.apply(self.d.apply(acquired))
        self.p.start_prefill(acquire.request)
        self.d.cancel_local(acquire.request, "client disconnected")
        self.d.begin_drain(acquire.request)
        done = self.d.finish_drain(acquire.request, reason="cancelled")
        self.assertIsNone(self.p.apply(done))
        self.assertNotIn(7, self.p.available_slots())
        self.assertEqual(self.p.state(acquire.request), "CANCELLING")
        with self.assertRaises(ValueError):
            self.p.publish_kv_ready(acquire.request, 32)
        release_ack = self.p.finish_prefill_writes(acquire.request)
        self.assertEqual(release_ack.kind, MessageType.RELEASE_ACK)
        self.assertIn(7, self.p.available_slots())
        self.d.apply(release_ack)
        self.assertEqual(self.d.state(acquire.request), "CLOSED")

    def test_kv_ready_requires_an_explicit_write_drain(self):
        """An acquired binding alone never asserts that BM data is readable."""
        acquire = self.d.acquire_decode(27, "writes", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, 7)))
        self.p.start_prefill(acquire.request)
        with self.assertRaisesRegex(ValueError, "writes"):
            self.p.publish_kv_ready(acquire.request, 32)
        self.p.finish_prefill_writes(acquire.request)
        self.assertEqual(
            self.p.publish_kv_ready(acquire.request, 32).kind, MessageType.KV_READY
        )

    def test_kv_ready_before_existing_transfer_still_waits(self):
        """The original Index K transfer remains an independent decode gate."""
        acquire = self.d.acquire_decode(27, "kv-first", 5, 32, 16, "tcp://d:4351")
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, 7)))
        self.p.start_prefill(acquire.request)
        self.p.finish_prefill_writes(acquire.request)
        self.d.apply(self.p.publish_kv_ready(acquire.request, 32))
        self.assertFalse(self.d.can_decode(acquire.request))
        with self.assertRaisesRegex(ValueError, "missing"):
            self.d.start_decode(acquire.request)
        self.d.transfer_succeeded(acquire.request)
        self.assertTrue(self.d.can_decode(acquire.request))

    def test_prefill_rejects_acquire_over_local_capacity(self):
        """P independently rejects malformed lengths before reserving DRAM."""
        request = RequestIdentity(27, "too-large", "p-boot", "d-boot")
        acquire = MempoolMessage(
            MessageType.ACQUIRE,
            request=request,
            d_slot=SlotLease(5, 1),
            reply_to="tcp://d:4351",
            prompt_tokens=8193,
            decode_tokens=16,
        )
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.p.apply(acquire)
        self.assertIn(7, self.p.available_slots())


if __name__ == "__main__":
    unittest.main()
