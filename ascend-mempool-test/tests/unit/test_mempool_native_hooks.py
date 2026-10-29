"""Execute the real generic allocation hooks at the CPU allocator boundary."""

import dataclasses
import unittest
from array import array
from collections import deque
from types import SimpleNamespace

import test_mempool_subrouter as sr_fixture
import torch
from test_mempool_native_reservation import Pages, Rows, request
from test_native_release import load_functions, load_methods

from ascend_mempool_pd.mempool_native_reservation import NativeReservations
from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestNativeHooks(unittest.TestCase):
    def test_pd_loops_return_for_graceful_shutdown(self):
        for role, owner in (
            ("prefill", "SchedulerDisaggregationPrefillMixin"),
            ("decode", "SchedulerDisaggregationDecodeMixin"),
        ):
            methods = [
                f"event_loop_{mode}_disagg_{role}" for mode in ("normal", "overlap")
            ]
            namespace = dict(torch=torch, deque=deque)
            cls = load_methods(f"disaggregation/{role}.py", owner, methods, namespace)
            worker = cls()
            worker.gracefully_exit = True
            for name in methods:
                with self.subTest(role=role, loop=name):
                    getattr(worker, name)()

    def test_fake_warmup_uses_native_pages_and_returns_them_before_real_reserve(self):
        worker = self.make_warmup_worker()
        rows, pages, req = worker.rows, worker.pages, worker.req
        queue, batch, namespace = worker.queue, worker.batch, worker.namespace
        native = rows.mempool_reservations
        before = pages.available_size()
        transfer = queue._pre_alloc(req)
        self.assertEqual(transfer.tolist(), [128, 129, 130, 131])
        self.assertEqual(pages.available_size(), before - 128)
        self.assertFalse(native.reservations)

        for pos in range(4, 11):
            batch.seq_lens = batch.seq_lens_cpu = torch.tensor([pos])
            self.assertTrue(batch.check_decode_mem())
            self.assertEqual(
                namespace["alloc_for_decode"](batch, 1).tolist(), [128 + pos]
            )
        namespace["release_kv_cache"](req, batch.tree_cache)
        self.assertEqual(pages.available_size(), before)
        self.assertEqual(rows.free_slots, [1])
        self.assertTrue(req.kv.is_kv_released)
        self.assertFalse(native.reservations)
        real = request(1)
        real.bootstrap_host = "p"
        with self.assertRaises(KeyError):
            queue._pre_alloc(real)
        batch.reqs = [real]
        self.assertFalse(batch.check_decode_mem())
        with self.assertRaisesRegex(RuntimeError, "reservation"):
            namespace["alloc_for_decode"](batch, 1)
        batch.reqs = [req, real]
        with self.assertRaisesRegex(RuntimeError, "share a batch"):
            batch.check_decode_mem()
        native.reserve(1, 4096, 128)
        native.attach(real, 4096)
        native.release(real)
        self.assertEqual(pages.available_size(), before)

    @staticmethod
    def make_warmup_worker():
        rows, pages = Rows(), Pages()
        rows.mempool_reservations = NativeReservations(rows, pages)
        req = request(0)
        req.bootstrap_host = "2.2.2.2"
        req.origin_input_ids = [10, 11, 12, 13]
        req.output_ids = []
        req.kv.holds_kv, req.kv.is_kv_released = True, False
        req.kv.cache_protected_len = 0
        req.owned_kv_len = lambda: req.kv.kv_committed_len

        def release():
            req.kv.holds_kv, req.kv.is_kv_released = False, True

        def alloc_rows(reqs):
            result = rows.alloc_rows(len(reqs))
            for item, row in zip(reqs, result):
                item.kv.req_pool_idx = row
            return result

        req.kv.mark_kv_released = release
        rows.alloc = alloc_rows
        rows.alloc_aux_to_lengths = lambda **kw: None
        pages.device = "cpu"
        pages.alloc_extend = lambda **kw: pages.alloc(128)[: kw["extend_num_tokens"]]
        pages.check_decode_capacity = lambda **kw: (
            pages.available_size() >= kw["num_tokens"]
        )
        namespace = dict(
            torch=torch,
            dataclasses=dataclasses,
            get_disagg=lambda: SimpleNamespace(
                disaggregation_decode_enable_radix_cache=False
            ),
            _is_npu=False,
            _alloc_page_size=lambda batch: 128,
            alloc_paged_token_slots_decode=lambda **kw: kw["last_loc"] + 1,
            HybridReqToTokenPool=type("UnusedHybridPool", (), {}),
            get_spec=lambda: SimpleNamespace(speculative_algorithm=None),
            get_serving=lambda: SimpleNamespace(strip_thinking_cache=False),
            ceil_align=lambda n, size: (n + size - 1) // size * size,
        )
        load_functions(
            "disaggregation/decode.py", {"alloc_for_decode_prealloc"}, namespace
        )
        load_functions("mem_cache/allocation.py", {"alloc_for_decode"}, namespace)
        load_functions(
            "mem_cache/common.py",
            {"release_kv_cache", "_release_overallocated_kv_indices"},
            namespace,
        )
        queue_type = load_methods(
            "disaggregation/decode.py",
            "DecodePreallocQueue",
            {"_pre_alloc", "_pre_alloc_fill_len"},
            namespace,
            standalone=True,
        )
        queue = queue_type()
        queue.req_to_token_pool, queue.token_to_kv_pool_allocator = rows, pages
        queue.scheduler = SimpleNamespace(enable_hisparse=False)
        queue._required_alloc_tokens = lambda **kw: 128
        queue._uses_swa_tail_prealloc = lambda: False
        queue._swa_tail_len = lambda length: length
        cache_type = load_methods(
            "mem_cache/chunk_cache.py",
            "ChunkCache",
            {"cache_finished_req"},
            namespace,
            standalone=True,
        )
        cache = cache_type()
        cache.req_to_token_pool, cache.token_to_kv_pool_allocator = rows, pages
        cache.free_kv_row = lambda kv, ranges: [
            pages.free(rows.req_to_token[kv.req_pool_idx, start:end])
            for start, end in ranges
        ]
        batch_type = load_methods(
            "managers/schedule_batch.py",
            "ScheduleBatch",
            {"check_decode_mem"},
            namespace,
            standalone=True,
        )
        batch = batch_type()
        batch.reqs, batch.req_to_token_pool = [req], rows
        batch.token_to_kv_pool_allocator, batch.tree_cache = pages, cache
        batch.spec_algorithm = None
        batch.new_tokens_required_next_decode = lambda selected: 1
        batch.maybe_evict_swa = lambda: None
        batch.req_pool_indices = batch.req_pool_indices_cpu = torch.tensor([1])
        batch.model_config = SimpleNamespace(is_encoder_decoder=False)
        return SimpleNamespace(
            rows=rows,
            pages=pages,
            req=req,
            queue=queue,
            batch=batch,
            namespace=namespace,
        )

    def test_maximum_request_keeps_full_generation_budget_in_native_scheduler(self):
        config = RouterConfig("p", "d", "/model")
        namespace = dict(
            get_parallel=lambda: SimpleNamespace(attn_dcp_size=1),
            get_schedule=lambda: SimpleNamespace(
                max_prefill_tokens=8192, max_queued_requests=16
            ),
        )
        worker_type = load_methods(
            "managers/tp_worker.py",
            "TpModelWorker",
            {"get_worker_info"},
            namespace,
            standalone=True,
        )
        scheduler_type = load_methods(
            "managers/scheduler.py",
            "Scheduler",
            {"init_req_max_new_tokens"},
            namespace,
            standalone=True,
        )
        worker = worker_type()
        worker.model_config = SimpleNamespace(context_len=config.context_length)
        worker.model_runner = SimpleNamespace(
            effective_max_total_num_tokens=config.max_total_tokens,
            max_total_num_tokens=config.max_total_tokens,
            req_to_token_pool=SimpleNamespace(
                schedulable_token_capacity=lambda count: count,
                size=16,
                max_context_len=config.context_length,
            ),
            token_to_kv_pool=SimpleNamespace(size=config.max_total_tokens),
            max_running_requests=16,
            forward_stream=None,
        )
        worker.random_seed, worker.device = 0, "cpu"
        scheduler = scheduler_type()
        scheduler.max_req_len = worker.get_worker_info()[4]
        scheduler.max_new_tokens_limit = scheduler.sliding_window_size = None
        scheduler.chunked_prefill_size = -1
        scheduler.max_total_num_tokens = config.max_total_tokens
        scheduler.token_to_kv_pool_allocator = SimpleNamespace(
            max_new_tokens_for_memory=lambda input_len, limit, **kw: limit
        )
        req = SimpleNamespace(
            origin_input_ids=[7] * 8192,
            sampling_params=SimpleNamespace(max_new_tokens=128, min_new_tokens=0),
        )
        scheduler.init_req_max_new_tokens(req)
        self.assertEqual(req.sampling_params.max_new_tokens, 128)
        with self.assertRaisesRegex(ValueError, "scheduler"):
            RouterConfig("p", "d", "/model", context_length=8320)

    def test_prefill_validates_dispatch_before_rewriting_generation_limit(self):
        system = sr_fixture.TestSubrouter()
        system.setUp()
        self.addCleanup(system.doCleanups)
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
        shard = system.ticks["prefill"][0].shard
        namespace = dict(
            is_unadmitted_reject=lambda req: False,
            FAKE_BOOTSTRAP_HOST="2.2.2.2",
            KVClassType=SimpleNamespace(SENDER="sender"),
            get_kv_class=lambda backend, kind: lambda **kw: SimpleNamespace(**kw),
        )
        queue_type = load_methods(
            "disaggregation/prefill.py",
            "PrefillBootstrapQueue",
            {"add", "create_sender", "_process_req"},
            namespace,
            standalone=True,
        )
        queue = queue_type()
        queue.scheduler = SimpleNamespace(
            mempool_service=SimpleNamespace(track=shard.track)
        )
        queue.queue = []
        queue._check_if_req_exceed_kv_capacity = lambda req: False
        queue.transfer_backend, queue.kv_manager = "ascend", object()
        queue.tp_rank = queue.pp_rank = 0
        queue.bootstrap_port = 8995
        req = SimpleNamespace(
            rid="r1",
            bootstrap_room=1,
            bootstrap_host="p",
            disagg_prefill_dp_rank=None,
            origin_input_ids=array("q", [7] * 128),
            sampling_params=SimpleNamespace(
                max_new_tokens=4, top_k=1, ignore_eos=False
            ),
        )
        queue.add(req, 1)
        self.assertIs(shard.req, req)
        self.assertEqual(req.sampling_params.max_new_tokens, 1)
        self.assertEqual(shard.request["max_new_tokens"], 4)
        self.assertEqual(queue.queue, [req])

    def test_prealloc_and_decode_consume_reserved_indices_with_no_free_capacity(self):
        namespace = {"torch": torch, "dataclasses": dataclasses}
        namespace["ReqToTokenPool"] = load_methods(
            "mem_cache/memory_pool.py",
            "ReqToTokenPool",
            {"alloc_rows", "free_rows"},
            namespace,
        )
        pool_type = load_methods(
            "disaggregation/decode.py",
            "DecodeReqToTokenPool",
            {"alloc_rows", "free_rows", "write", "available_size", "free"},
            namespace,
        )
        rows = pool_type()
        rows.free_slots = [1]
        rows._on_alloc_rows = rows._aux_cache = None
        rows.req_generation = torch.zeros(2, dtype=torch.int64)
        rows.req_to_token = torch.zeros((2, 8320), dtype=torch.int32)
        pages = Pages()
        pages.pages.pop()  # Exactly 33 discontiguous pages; all will be reserved.
        native = rows.mempool_reservations = NativeReservations(rows, pages)
        native.reserve(1, 4096, 128)
        self.assertEqual(rows.available_size(), 0)
        self.assertEqual(pages.available_size(), 0)
        self.assertEqual(rows.req_generation.tolist(), [0, 1])

        queue_type = load_methods(
            "disaggregation/decode.py",
            "DecodePreallocQueue",
            {"_pre_alloc", "_pre_alloc_fill_len"},
            namespace,
            standalone=True,
        )
        queue = queue_type()
        queue.req_to_token_pool = rows
        req = request()
        req.kv.holds_kv = True
        transfer = queue._pre_alloc(req)
        self.assertTrue(torch.equal(transfer, rows.req_to_token[1, :4096]))
        self.assertEqual(req.full_untruncated_fill_ids, req.origin_input_ids)

        batch_type = load_methods(
            "managers/schedule_batch.py",
            "ScheduleBatch",
            {"check_decode_mem"},
            namespace,
            standalone=True,
        )
        batch = batch_type()
        batch.reqs, batch.req_to_token_pool = [req], rows
        batch.seq_lens_cpu = torch.tensor([4096])
        self.assertTrue(batch.check_decode_mem())
        load_functions("mem_cache/allocation.py", {"alloc_for_decode"}, namespace)
        self.assertEqual(namespace["alloc_for_decode"](batch, 1).tolist(), [8320])
        self.assertEqual(req.kv.kv_allocated_len, 4097)
        self.assertEqual(pages.available_size(), 0)
        native.release(req)
        self.assertEqual(pages.available_size(), 33 * 128)
        self.assertEqual(rows.available_size(), 1)
        self.assertIsNone(req.kv.req_pool_idx)
