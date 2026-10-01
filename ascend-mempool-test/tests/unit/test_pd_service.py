"""Verify real Req projection and native ownership through the service boundary."""

import sys
import unittest
from types import SimpleNamespace

import test_runtime
import torch
from test_runtime import prefill_batch

import ascend_mempool.runtime

sys.modules["sglang.srt.hardware_backend.npu.mempool.runtime"] = ascend_mempool.runtime

from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_protocol import PoolDescriptor, PoolPeer
from ascend_mempool_pd.mempool_service import MempoolPDService


class TestMempoolPDService(unittest.TestCase):
    """Use actual runtime/control, injecting only transport, device and native free."""

    def setUp(self):
        """Construct one pair for local service contracts; TP tests cover consensus."""
        helper = test_runtime.TestMempoolRuntime()
        self.addCleanup(helper.doCleanups)
        self.runtime, self.targets, self.events = helper.make_runtime(layers=1)
        layout = PoolDescriptor(1, 16, 8, 8, 1, 4, "bfloat16", 2**30, 2**30, 2**30)
        self.p = MempoolPDControl(PoolPeer("p", "prefill", 0, 16, 1, 7, layout))
        self.d = MempoolPDControl(PoolPeer("d", "decode", 0, 16, 1, 7, layout))
        self.d.apply(self.p.apply(self.d.begin_handshake("tcp://d:1")))
        self.freed = []
        self.service = MempoolPDService(
            self.runtime,
            self.p,
            gather=lambda value: [value] * 16,
            send=lambda endpoint, message: self.d.enqueue(message),
            reply_to="tcp://p:1",
            endpoint="tcp://d:1",
            native_release=lambda req, insert: self.freed.append((req.rid, insert)),
            drain_host=lambda: None,
            synchronize=lambda: None,
            cancel_request=lambda req: None,
            clock=lambda: 1.0,
        )

    @staticmethod
    def request(room, row):
        """Keep the actual nested Req KV field and the existing fake marker."""
        return SimpleNamespace(
            rid=f"req-{room}",
            bootstrap_room=room,
            bootstrap_host="10.0.0.1",
            origin_input_ids=[1, 2],
            sampling_params=SimpleNamespace(max_new_tokens=4),
            kv=SimpleNamespace(req_pool_idx=row),
            finished=lambda: False,
        )

    def bind_request(self, req, slot):
        """Establish protocol approval using the public single-pair control API."""
        self.service.track(req)
        message = self.d.acquire_decode(
            req.bootstrap_room, req.rid, slot, 2, 4, "tcp://d:1"
        )
        self.p.apply(message)
        self.p.apply(self.d.apply(self.p.acquire_prefill(message.request, slot)))
        self.p.start_prefill(message.request)
        return message.request

    def test_p_native_release_waits_for_handoff_and_write_completion(self):
        """KV_READY does not free native storage; detach preserves the persistent slot."""
        req = self.request(1, 1)
        identity = self.bind_request(req, 0)
        batch = SimpleNamespace(
            reqs=[req],
            forward_mode=SimpleNamespace(
                is_decode=lambda: False,
                is_idle=lambda: False,
                is_prebuilt=lambda: False,
            ),
            extend_lens=[2],
            prefix_lens=[0],
        )
        self.service.prepare_batch(batch)
        with self.runtime.forward_scope():
            k = torch.ones((2, 2), dtype=torch.bfloat16)
            self.runtime.write_layer(5, k, k, prefill_batch((2,), (0,), (1,)))
        self.service.advance()
        self.assertEqual(self.freed, [])
        self.events[-1].done = True
        self.service.advance()
        self.assertEqual(self.p.state(identity), "WAITING_DONE")
        self.assertEqual(self.freed, [])
        self.assertTrue(self.service.defer_release(req, handoff=True))
        self.service.advance()
        self.assertEqual(self.freed, [(req.rid, True)])
        self.assertNotIn(0, self.p.available_slots())
        self.assertEqual(self.runtime.row_slot[1].item(), -1)
        self.service.advance()
        self.assertEqual(len(self.freed), 1)
        # Reuse the native row while the earlier prompt stays in its P slot.
        next_req = self.request(3, 1)
        self.bind_request(next_req, 1)
        batch.reqs = [next_req]
        self.service.prepare_batch(batch)
        with self.runtime.forward_scope():
            self.runtime.write_layer(5, k, k, prefill_batch((2,), (0,), (1,)))
        self.assertEqual(self.runtime.row_slot[1].item(), 1)
        self.assertNotIn(0, self.p.available_slots())
        self.assertEqual(self.p.state(identity), "WAITING_DONE")

    def test_cancelled_transfer_holds_until_native_ack_without_repeated_drain(self):
        """Cancellation fences once, then waits for the actual remote writer ACK."""
        helper = test_runtime.TestMempoolRuntime()
        self.addCleanup(helper.doCleanups)
        runtime, _, _ = helper.make_runtime(rank=1, layers=1)
        effects = []
        service = MempoolPDService(
            runtime,
            self.d,
            gather=lambda value: [value] * 16,
            send=lambda endpoint, message: None,
            reply_to="tcp://d:1",
            endpoint="tcp://p:1",
            native_release=lambda req, insert: effects.append("free"),
            drain_host=lambda: effects.append("host-drain"),
            synchronize=lambda: effects.append("device-drain"),
            cancel_request=lambda req: None,
            clock=lambda: 1.0,
        )
        req = self.request(9, 1)
        service.track(req)
        acquire = self.d.acquire_decode(9, "cancel", 0, 2, 4, "tcp://d:1")
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, 0)))
        drained = False
        service.kv_manager = SimpleNamespace(
            is_abort_release_safe=lambda room, count: drained
        )
        receiver = SimpleNamespace(
            bootstrap_infos=[object()], abort=lambda: effects.append("native-abort")
        )
        service.native_failure(req, SimpleNamespace(kv_receiver=receiver))
        for _ in range(4):
            service.advance()
        self.assertEqual(effects, ["native-abort", "host-drain", "device-drain"])
        self.assertNotIn(0, self.d.available_slots())
        drained = True
        service.advance()
        self.assertEqual(effects[-1], "free")
        self.assertIn(0, self.d.available_slots())
        service.advance()
        self.assertEqual(effects.count("free"), 1)

    def test_p_native_failure_before_acquire_still_drains_and_retires(self):
        """A sender can time out before D publishes any mempool ACQUIRE."""
        req = self.request(21, None)
        req.disagg_kv_sender = SimpleNamespace(abort=lambda: None)
        self.service.track(req)
        self.service.kv_manager = SimpleNamespace(
            mempool_prefill_transfer_drained=lambda room: True
        )
        effects = []
        self.service._drain_host = lambda: effects.append("drain")
        self.service.native_failure(req)
        self.service.advance()
        self.assertEqual(effects, ["drain"])
        self.assertEqual(self.freed, [])
        self.service.advance()
        self.assertEqual(self.freed, [(req.rid, False)])
        self.assertFalse(self.service.has_pending_work())

    def test_p_cleanup_stops_polling_cleared_sender_status(self):
        """A cancelled P slot can outlive its already-cleared native sender."""
        from unittest.mock import patch

        req = self.request(22, None)
        self.bind_request(req, 0)
        statuses = {22: True}
        req.disagg_kv_sender = SimpleNamespace(
            abort=lambda: None, clear=lambda: statuses.pop(22)
        )
        self.service.kv_manager = SimpleNamespace(
            mempool_prefill_transfer_drained=lambda room: statuses[room]
        )
        self.service.native_failure(req)
        self.service.scheduler = SimpleNamespace(
            req_to_metadata_buffer_idx_allocator=None
        )
        with patch.dict(
            sys.modules,
            {
                "sglang.srt.disaggregation.prefill": SimpleNamespace(
                    maybe_release_metadata_buffer=lambda *args: None
                ),
                "sglang.srt.mem_cache.common": SimpleNamespace(
                    release_kv_cache=lambda *args, **kwargs: None
                ),
            },
        ):
            self.service._free_native(req, False)
        self.assertEqual(statuses, {})
        self.service.scheduler = None
        self.service.advance()
        self.assertNotIn(0, self.p.available_slots())

    def test_cancelled_d_cleanup_resets_metadata_before_reuse(self):
        """Retained cleanup preserves the original metadata and staging teardown."""
        from unittest.mock import patch

        req = self.request(12, None)
        service = self.service
        service.control = self.d
        service.track(req)
        effects = []
        receiver = SimpleNamespace(
            abort=lambda: None, clear=lambda: effects.append("clear")
        )
        dr = SimpleNamespace(kv_receiver=receiver, metadata_buffer_index=0)
        service.native_failure(req, dr)
        rooms = [12]
        queue = SimpleNamespace(
            enable_staging=True,
            staging_handler=SimpleNamespace(
                is_staging_room=lambda room: True,
                unregister_decode_req=lambda room: effects.append("unregister"),
            ),
            metadata_buffers=SimpleNamespace(bootstrap_room=rooms),
        )
        service.scheduler = SimpleNamespace(
            disagg_decode_transfer_queue=queue,
            req_to_metadata_buffer_idx_allocator=SimpleNamespace(
                free=lambda idx: effects.append(("free-meta", rooms[idx]))
            ),
        )
        service.kv_manager = SimpleNamespace(
            clear_deferred_abort_state=lambda room: effects.append("clear-acks")
        )
        with patch.dict(
            sys.modules,
            {
                "sglang.srt.disaggregation.prefill": SimpleNamespace(
                    maybe_release_metadata_buffer=lambda *args: None
                ),
                "sglang.srt.mem_cache.common": SimpleNamespace(
                    release_kv_cache=lambda *args, **kwargs: None
                ),
            },
        ):
            service._free_native(req, False)
        self.assertEqual(
            effects, ["unregister", "clear", ("free-meta", 0), "clear-acks"]
        )
        self.assertIsNone(dr.kv_receiver)
        self.assertEqual(dr.metadata_buffer_index, -1)

    def test_zero_decode_still_drains_before_native_free_and_done(self):
        """A first-token finish owns D native pages even without a model forward."""
        helper = test_runtime.TestMempoolRuntime()
        self.addCleanup(helper.doCleanups)
        runtime, _, _ = helper.make_runtime(rank=1, layers=1)
        effects = []
        sent = []
        service = MempoolPDService(
            runtime,
            self.d,
            gather=lambda value: [value] * 16,
            send=lambda endpoint, message: sent.append(message),
            reply_to="tcp://d:1",
            endpoint="tcp://p:1",
            native_release=lambda req, insert: effects.append("free"),
            drain_host=lambda: effects.append("host-drain"),
            synchronize=lambda: effects.append("device-drain"),
            cancel_request=lambda req: None,
            clock=lambda: 1.0,
        )
        req = self.request(7, 1)
        service.track(req)
        acquire = self.d.acquire_decode(7, "zero", 0, 2, 4, "tcp://d:1")
        self.p.apply(acquire)
        self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, 0)))
        self.p.start_prefill(acquire.request)
        self.p.finish_prefill_writes(acquire.request)
        self.d.apply(self.p.publish_kv_ready(acquire.request, 2))
        self.assertFalse(service.transfer_complete(req))
        service.advance()
        service.advance()
        self.assertTrue(service.transfer_complete(req))
        service.defer_release(req)
        service.advance()
        self.assertEqual(effects, ["host-drain", "device-drain"])
        self.assertNotIn(0, self.d.available_slots())
        service.advance()
        self.assertEqual(effects, ["host-drain", "device-drain", "free"])
        self.assertIn(0, self.d.available_slots())
        service.advance()
        self.assertEqual(effects.count("free"), 1)
        from ascend_mempool_pd.mempool_protocol import MessageType

        self.assertEqual(len([m for m in sent if m.kind == MessageType.DONE]), 1)

    def test_overlap_result_and_delayed_sampling_finish_before_release(self):
        """Host drain can discover another finished request before the single fence."""
        from collections import deque

        helper = test_runtime.TestMempoolRuntime()
        self.addCleanup(helper.doCleanups)
        runtime, _, _ = helper.make_runtime(rank=1, layers=1)
        effects = []
        service = MempoolPDService(
            runtime,
            self.d,
            gather=lambda value: [value] * 16,
            send=lambda *args: None,
            reply_to="tcp://d:1",
            endpoint="tcp://p:1",
            native_release=lambda req, insert: effects.append(("free", req.rid)),
            drain_host=lambda: service._flush_scheduler(),
            synchronize=lambda: effects.append("device-drain"),
            cancel_request=lambda req: None,
            clock=lambda: 1.0,
        )
        reqs = [self.request(30 + slot, slot + 1) for slot in range(2)]
        for slot, req in enumerate(reqs):
            service.track(req)
            acquire = self.d.acquire_decode(
                req.bootstrap_room, req.rid, slot, 2, 4, "tcp://d:1"
            )
            self.p.apply(acquire)
            self.p.apply(self.d.apply(self.p.acquire_prefill(acquire.request, slot)))
            self.p.start_prefill(acquire.request)
            self.p.finish_prefill_writes(acquire.request)
            self.d.apply(self.p.publish_kv_ready(acquire.request, 2))
            self.d.transfer_succeeded(acquire.request)
            self.d.start_decode(acquire.request)
            service.transfer_complete(req)

        def finish_result(batch, result):
            """A real result callback only records pending release."""
            effects.append("result")
            service.defer_release(reqs[1])

        scheduler = SimpleNamespace(
            result_queue=deque([(object(), object())]),
            launch_batch_sample_if_needed=lambda result, batch: effects.append(
                "sample"
            ),
            process_batch_result=finish_result,
            last_batch=object(),
            cur_batch_for_debug=object(),
            chunked_req=None,
            running_batch=SimpleNamespace(
                filter_batch=lambda: effects.append("filter")
            ),
        )
        service.scheduler = scheduler
        service.defer_release(reqs[0])
        service.advance()
        self.assertEqual(effects, ["sample", "result", "filter", "device-drain"])
        self.assertIsNone(scheduler.last_batch)
        self.assertFalse(scheduler.result_queue)
        service.advance()
        self.assertEqual(effects[-2:], [("free", "req-30"), ("free", "req-31")])
        self.assertEqual(effects.count("device-drain"), 1)
        self.assertEqual(len(self.d.available_slots()), 16)

    def test_missing_real_binding_is_rejected_but_existing_fake_marker_is_allowed(self):
        """A padding mask must not turn an unapproved real request into silent success."""
        req = self.request(2, 1)
        self.service.track(req)
        batch = SimpleNamespace(
            reqs=[req],
            forward_mode=SimpleNamespace(
                is_decode=lambda: False,
                is_idle=lambda: False,
                is_prebuilt=lambda: False,
            ),
            extend_lens=[2],
            prefix_lens=[0],
        )
        with self.assertRaisesRegex(RuntimeError, "approval"):
            self.service.prepare_batch(batch)
        req.bootstrap_host = "2.2.2.2"
        self.service.prepare_batch(batch)
        with self.runtime.forward_scope():
            k = torch.ones((2, 2), dtype=torch.bfloat16)
            self.runtime.write_layer(5, k, k, prefill_batch((2,), (0,), (1,)))
        self.events[-1].done = True
        self.runtime.poll_completed()
        self.assertEqual(self.targets[0][0, 0].flatten().tolist(), [-1] * 4)
