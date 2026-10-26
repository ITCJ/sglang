"""Full TP service lifecycle with actual runtime and native reservation owners."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import test_mempool_subrouter as sr_fixture
import test_pd_service as service_fixture
import test_prefix as prefix_fixture
import test_runtime as runtime_fixture
import torch

from ascend_mempool.runtime import MempoolRuntime
from ascend_mempool_pd.mempool_service import MempoolPDService


class TestRouterService(unittest.TestCase):
    def test_real_write_receipt_survives_native_cleanup_and_zero_decode_free(self):
        self.lifecycle(decode_forward=False)

    def test_each_decode_step_records_actual_resident_and_executing_counts(self):
        self.lifecycle(decode_forward=True)

    def test_warmup_native_release_admit_and_alive_free_keep_both_prefix_regions(self):
        self.lifecycle(decode_forward=True, prefix=True)

    def lifecycle(self, *, decode_forward, prefix=False):
        system = sr_fixture.TestSubrouter()
        system.setUp()
        self.addCleanup(system.doCleanups)
        limit = 2 if decode_forward else 1
        system.request.update(input_ids=[7, 8], prompt_tokens=2, max_new_tokens=limit)
        services, requests, events = {}, {}, {}
        stores, copies, copy_events = [], [], []
        for role in ("prefill", "decode"):
            services[role], requests[role], events[role] = [], [], []
            for rank, tick in enumerate(system.ticks[role]):
                helper = runtime_fixture.TestMempoolRuntime()
                self.addCleanup(helper.doCleanups)
                runtime, _, device_events = helper.make_runtime(
                    rank=0 if role == "prefill" else 1, layers=1
                )
                shard = tick.shard
                native_index = None
                if prefix and role == "prefill":
                    helper = prefix_fixture.TestPrefixStore()
                    self.addCleanup(helper.doCleanups)
                    store, manager, kernel, device_events = helper.make_store(
                        verify=True
                    )
                    store.replica = rank
                    stores.append(store)
                    copies.append(kernel)
                    copy_events.append(device_events)
                    native_index = torch.tensor(
                        [[1, 2], [11, 12], [21, 22], [31, 32]], dtype=torch.bfloat16
                    )
                    native_pool = SimpleNamespace(
                        indexer_layer_ids=(5,),
                        index_head_dim=2,
                        index_k_scale_buffer=None,
                        get_index_k_buffer=lambda layer, data=native_index: data,
                    )

                    def make_event(events=device_events):
                        event = runtime_fixture.ManualEvent()
                        events.append(event)
                        return event

                    runtime = MempoolRuntime(
                        manager,
                        req_pool_rows=9,
                        max_context_len=16,
                        start_layer=5,
                        device="cpu",
                        kernels=[kernel] * 2,
                        index_pool=native_pool,
                        index_kernels=[kernel],
                        prefix_store=store,
                        event_factory=make_event,
                    )
                    shard.prefix = store

                def free(req, insert, shard=shard, native_index=native_index):
                    if shard.native is not None:
                        shard.native.release(req)
                    else:
                        req.kv.req_pool_idx = None
                        if native_index is not None:
                            native_index.zero_()

                service = MempoolPDService(
                    runtime,
                    shard.control,
                    gather=tick.gather,
                    send=lambda endpoint, msg: None,
                    reply_to="tcp://local:1",
                    endpoint="tcp://peer:1",
                    native_release=free,
                    drain_host=lambda: None,
                    synchronize=lambda: None,
                    cancel_request=lambda req: None,
                    clock=lambda: 1.0,
                )
                service.subrouter = tick
                shard.effect = service._effect
                services[role].append(service)
                events[role].append(device_events)
                req = service_fixture.TestMempoolPDService.request(
                    1, 1 if role == "prefill" else None
                )
                req.rid = "r1"
                req.origin_input_ids = [7, 8]
                req.output_ids = []
                req.sampling_params = SimpleNamespace(
                    max_new_tokens=limit, top_k=1, ignore_eos=False
                )
                req.set_extend_range = lambda start, end: None
                req.kv.mark_kv_released = lambda: None
                requests[role].append(req)

        def advance(role):
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(lambda service: service.advance(), services[role]))

        # Native startup warmup stays local, then the same workers run a real Req.
        for role in services:
            for rank, service in enumerate(services[role]):
                fake = service_fixture.TestMempoolPDService.request(0, 1)
                fake.bootstrap_host = "2.2.2.2"
                service.track(fake)
                self.assertFalse(service.tracks(fake))
                self.assertIsNone(service.subrouter.shard.req)
                self.assertTrue(service.can_prefill(fake))
                self.assertTrue(service.can_preallocate(fake))
                self.assertTrue(service.transfer_complete(fake))
                batch = runtime_fixture.prefill_batch((2,), (0,), (1,))
                batch.reqs = [fake]
                batch.forward_mode.is_idle = lambda: False
                batch.forward_mode.is_prebuilt = lambda: False
                service.prepare_batch(batch)
                with service.runtime.forward_scope():
                    k = torch.ones((2, 2), dtype=torch.bfloat16)
                    for layer in range(5, 5 + service.runtime.layout.layers):
                        service.runtime.write_layer(layer, k, k, batch)
                events[role][rank][-1].done = True
                service.runtime.poll_completed()
                self.assertFalse(service.defer_release(fake, handoff=role == "prefill"))
                self.assertEqual(len(service.control.available_slots()), 16)
                self.assertEqual(service.runtime.row_slot[1].item(), -1)
                self.assertFalse(service._requests)
                if role == "decode":
                    self.assertFalse(service.subrouter.shard.native.reservations)

        # A direct HTTP Req cannot create ownership or bypass dispatch.
        with self.assertRaisesRegex(ValueError, "dispatch"):
            services["decode"][0].track(requests["decode"][0])
        d = system.command("decode", "alloc", request=system.request)
        p = system.command(
            "prefill", "alloc", request=system.request, bindings=d["ranks"]
        )
        d = system.command(
            "decode", "dispatch", request=system.request, bindings=p["ranks"]
        )
        system.command(
            "prefill", "dispatch", request=system.request, bindings=d["ranks"]
        )
        for role in services:
            for service, req in zip(services[role], requests[role]):
                service.track(req)
                if role == "decode":
                    service.subrouter.shard.native.attach(req, 2)
                    self.assertFalse(service.transfer_complete(req))
        advance("prefill")
        for rank, service in enumerate(services["prefill"]):
            req = requests["prefill"][rank]
            batch = SimpleNamespace(
                reqs=[req],
                forward_mode=SimpleNamespace(
                    is_idle=lambda: False,
                    is_prebuilt=lambda: False,
                    is_decode=lambda: False,
                ),
                prefix_lens=[0],
                extend_lens=[2],
            )
            service.prepare_batch(batch)
            with service.runtime.forward_scope():
                k = torch.ones((2, 2), dtype=torch.bfloat16)
                inputs = runtime_fixture.prefill_batch((2,), (0,), (1,))
                inputs.out_cache_loc = torch.tensor([3, 1])
                for layer in range(5, 5 + service.runtime.layout.layers):
                    service.runtime.write_layer(layer, k, k, inputs)
            events["prefill"][rank][-1].done = True
            service.defer_release(req, handoff=True)
        advance("prefill")
        ready = next(
            m for m in system.messages["prefill"] if m["kind"] == "ready_tokens"
        )
        self.assertTrue(all(req.kv.req_pool_idx is None for req in requests["prefill"]))
        self.assertTrue(
            all(len(c.available_slots()) == 15 for c in system.controls["prefill"])
        )
        system.command("decode", "ready", bindings=ready["ranks"])
        advance("decode")
        for service, req in zip(services["decode"], requests["decode"]):
            self.assertTrue(service.transfer_complete(req))
            if decode_forward:
                service.subrouter.shard.native.decode(
                    SimpleNamespace(reqs=[req], seq_lens_cpu=torch.tensor([2])), 1
                )
                batch = runtime_fixture.prefill_batch((1,), (0,), (1,))
                batch.forward_mode = SimpleNamespace(
                    is_idle=lambda: False,
                    is_prebuilt=lambda: False,
                    is_decode=lambda: True,
                )
                batch.seq_lens, batch.reqs = torch.tensor([3]), [req]
                with self.assertLogs(
                    "ascend_mempool_pd.mempool_service", level="INFO"
                ) as logs:
                    service.prepare_batch(batch)
                self.assertIn(
                    "step=1 resident_requests=1 real_requests=1", logs.output[0]
                )
                with service.runtime.forward_scope():
                    k = torch.ones((1, 2), dtype=torch.bfloat16)
                    service.runtime.write_layer(5, k, k, batch)
                events["decode"][service.control.local.tp_rank][-1].done = True
            service.defer_release(req)
        for _ in range(3):
            advance("decode")
        self.assertTrue(any(m["kind"] == "finished" for m in system.messages["decode"]))
        self.assertTrue(
            all(not t.shard.native.reservations for t in system.ticks["decode"])
        )
        self.assertTrue(
            all(len(c.available_slots()) == 15 for c in system.controls["decode"])
        )
        if prefix:
            advance("prefill")
            system.op += 1
            system.commands["prefill"].append(
                dict(
                    run_id="cpu",
                    role="prefill",
                    kind="admit",
                    rid="r1",
                    room=1,
                    attempt="attempt",
                    op_id=system.op,
                    hashes=["page0"],
                    source_pages=[0],
                    sources=[
                        dict(rank=rank, slot=0, pages=[item["pages"][0]])
                        for rank, item in enumerate(p["ranks"])
                    ],
                )
            )
            advance("prefill")
            for device_events in copy_events[:-1]:
                device_events[-1].done = True
            advance("prefill")
            self.assertFalse(
                any(m["kind"] == "admit_ack" for m in system.messages["prefill"])
            )
            copy_events[-1][-1].done = True
            advance("prefill")
            self.assertTrue(
                any(m["kind"] == "admit_ack" for m in system.messages["prefill"])
            )
        for role in services:
            system.op += 1
            system.commands[role].append(
                dict(
                    run_id="cpu",
                    role=role,
                    kind="free",
                    rid="r1",
                    room=1,
                    attempt="attempt",
                    op_id=system.op,
                )
            )
            advance(role)
            self.assertTrue(
                all(len(c.available_slots()) == 16 for c in system.controls[role])
            )
        if prefix:
            for store, kernel in zip(stores, copies):
                self.assertEqual(store.free_pages, (1, 2))
                for region, count in (("main", 2), ("index", 1)):
                    for layer in range(count):
                        kernel.buffers[
                            store.manager.view(
                                0, layer, region="alive_" + region
                            ).device_base
                        ].zero_()
                index = kernel.buffers[
                    store.manager.view(0, 0, region="prefix_index").device_base
                ]
                self.assertEqual(index[0, :, 0].tolist(), [[31, 32], [11, 12]])
