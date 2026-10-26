"""Join real suffix writes and prefix copies at the scheduler service boundary."""

import unittest
from types import SimpleNamespace

import test_pd_service
import test_prefix
import torch
from test_mempool_native_prefix import native_methods
from test_runtime import ManualEvent, prefill_batch

from ascend_mempool.runtime import MempoolRuntime
from ascend_mempool_pd.mempool_control import MempoolPDControl
from ascend_mempool_pd.mempool_prefill_restore import PrefillRestore
from ascend_mempool_pd.mempool_protocol import PoolDescriptor, PoolPeer
from ascend_mempool_pd.mempool_router_protocol import native_wire
from ascend_mempool_pd.mempool_service import MempoolPDService
from ascend_mempool_pd.mempool_subrouter import MempoolSubrouter, RouterShard


class TestPrefixService(unittest.TestCase):
    def test_native_can_free_after_suffix_while_fill_still_blocks_ready(self):
        self.exercise_fill(delayed=True)

    def test_verified_fill_can_complete_before_native_free(self):
        self.exercise_fill(delayed=False)

    def exercise_fill(self, *, delayed):
        helper = test_prefix.TestPrefixStore()
        self.addCleanup(helper.doCleanups)
        store, manager, copy, events = helper.make_store(verify=True)
        store.begin_request("A", 3, 2)
        for region, layers in (("main", 2), ("index", 1)):
            for layer in range(layers):
                target = copy.buffers[
                    manager.view(0, layer, region="alive_" + region).device_base
                ]
                values = torch.full_like(target[3, :2], 17 + layer)
                target[3, :2] = values
                store.capture_source(
                    region,
                    layer,
                    values,
                    torch.full((2,), 3),
                    torch.arange(2),
                    torch.ones(2, dtype=torch.bool),
                )
        store.verify_alive(3, 2)
        save = dict(
            rid="A",
            op_id=100,
            slot=3,
            prompt_tokens=2,
            source_pages=[0],
            target_pages=[0],
        )
        store.admit(**save)
        events[-1].done = True
        store.admit(**save)
        store.release_source("A")
        buffers = [torch.ones((8, 1, 2), dtype=torch.bfloat16) for _ in range(5)]
        pool = SimpleNamespace(
            start_layer=5,
            layer_num=2,
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            indexer_layer_ids=(5,),
            index_head_dim=2,
            index_k_scale_buffer=None,
            page_size=2,
            get_key_buffer=lambda i: buffers[i - 5],
            get_value_buffer=lambda i: buffers[i - 3],
            get_index_k_buffer=lambda i: buffers[4],
        )
        pool = native_methods(pool)
        allocator = SimpleNamespace(
            page_size=2, alloc=lambda n: torch.tensor([2, 3]), get_kvcache=lambda: pool
        )
        restore = PrefillRestore(allocator, store, delay_fill=delayed)

        def event():
            e = ManualEvent()
            events.append(e)
            return e

        runtime = MempoolRuntime(
            manager,
            req_pool_rows=3,
            max_context_len=8,
            start_layer=5,
            device="cpu",
            kernels=[copy] * 2,
            index_pool=pool,
            index_kernels=[copy],
            prefix_store=store,
            event_factory=event,
        )
        layout = PoolDescriptor(2, 16, 8, 2, 1, 4, "bfloat16", 2**30, 2**30, 2**30)
        p = MempoolPDControl(
            PoolPeer("p", "prefill", 0, 16, 1, 7, layout), router_owned=True
        )
        d = MempoolPDControl(
            PoolPeer("d", "decode", 0, 16, 1, 7, layout), router_owned=True
        )
        d.apply(p.apply(d.begin_handshake("tcp://d:1")))
        freed = []

        def free(req, insert):
            freed.append(req.rid)
            req.kv.req_pool_idx = None
            for buffer in buffers:
                buffer.zero_()

        service = MempoolPDService(
            runtime,
            p,
            gather=lambda v: [v] * 16,
            send=lambda *args: None,
            reply_to="tcp://p:1",
            endpoint="tcp://d:1",
            native_release=free,
            drain_host=lambda: None,
            synchronize=lambda: None,
            cancel_request=lambda req: None,
            clock=lambda: 1.0,
        )
        shard = RouterShard(
            p,
            native=None,
            effect=service._effect,
            reply_to="tcp://p:1",
            page_size=2,
            prefix=store,
            restore=restore,
        )
        messages = []
        service.subrouter = MempoolSubrouter(
            shard,
            run_id="cpu",
            gather=lambda v: [v] * 16,
            receive=lambda: [],
            send=messages.append,
            registration=lambda: {},
            clock=lambda: 1.0,
        )
        request = dict(
            rid="B",
            room=1,
            attempt="one",
            input_ids=[7, 8, 9, 10],
            prompt_tokens=4,
            max_new_tokens=2,
            ignore_eos=False,
            hit_len=2,
            page_size=2,
        )

        def command(kind, op_id, **kw):
            return dict(
                run_id="cpu",
                role="prefill",
                rid="B",
                room=1,
                attempt="one",
                kind=kind,
                op_id=op_id,
                **kw,
            )

        def ranks(message):
            return [dict(rank=i, message=native_wire(message)) for i in range(16)]

        acquire = d.acquire_decode(1, "one", 0, 4, 2, "tcp://d:1")
        reply = shard.execute(
            command("alloc", 1, request=request, bindings=ranks(acquire)), 0
        )
        from ascend_mempool_pd.mempool_router_protocol import native_message

        bound = d.router_bind(native_message(reply["message"]))
        shard.execute(command("dispatch", 2, request=request, bindings=ranks(bound)), 0)
        sources = [
            dict(
                rank=i,
                slot=0,
                pages=[
                    dict(
                        pool="prefix",
                        region="p",
                        replica=i,
                        logical_page=0,
                        page_size=2,
                    )
                ],
            )
            for i in range(16)
        ]
        load = command("load_back", 3, hit_len=2, sources=sources)
        shard.execute(load, 0)
        events[-1].done = True
        shard.execute(load, 0)
        fill = command("fill", 4, hit_len=2, sources=sources)
        before_fill = len(events)
        shard.execute(fill, 0)
        self.assertEqual(len(events), before_fill + (0 if delayed else 1))
        if not delayed:
            self.assertFalse(restore.native_freed)
            events[-1].done = True
            shard.execute(fill, 0)
            self.assertTrue(shard.fill_complete)
            early_fill_event = events[-1]
        req = test_pd_service.TestMempoolPDService.request(1, 1)
        req.rid, req.origin_input_ids, req.output_ids = "B", [7, 8, 9, 10], []
        req.sampling_params = SimpleNamespace(
            max_new_tokens=2, top_k=1, ignore_eos=False
        )
        service.track(req)
        req.prefix_indices = restore.match(req)
        service.advance()
        batch = prefill_batch((2,), (2,), (1,))
        batch.reqs, batch.prefix_lens, batch.extend_lens = [req], [2], [2]
        batch.forward_mode.is_idle = batch.forward_mode.is_prebuilt = lambda: False
        batch.out_cache_loc = torch.tensor([5, 7])
        service.prepare_batch(batch)
        with runtime.forward_scope():
            for layer in (5, 6):
                runtime.write_layer(
                    layer,
                    torch.ones((2, 2), dtype=torch.bfloat16),
                    torch.ones((2, 2), dtype=torch.bfloat16),
                    batch,
                )
        events[-1].done = True
        service.defer_release(req, handoff=True)
        service.advance()
        self.assertEqual(freed, ["B"])
        self.assertTrue(restore.native_freed)
        self.assertEqual(
            any(m["kind"] == "ready_tokens" for m in messages), not delayed
        )
        service.advance()  # Publish native_freed before submitting the delayed fill.
        before_fill_retry = len(events)
        shard.execute(fill, 0)
        self.assertEqual(len(events), before_fill_retry + int(delayed))
        fill_event = events[-1] if delayed else early_fill_event
        self.assertEqual(fill_event.done, not delayed)
        fill_event.done = True
        shard.execute(fill, 0)
        service.advance()
        self.assertTrue(any(m["kind"] == "ready_tokens" for m in messages))
        self.assertEqual(freed, ["B"])


if __name__ == "__main__":
    unittest.main()
