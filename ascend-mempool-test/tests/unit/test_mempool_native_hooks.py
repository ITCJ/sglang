"""Execute the real generic allocation hooks at the CPU allocator boundary."""

import ast
import dataclasses
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace

import test_mempool_subrouter as sr_fixture
import torch
from test_mempool_native_reservation import Pages, request
from test_native_release import load_methods

from ascend_mempool_pd.mempool_native_reservation import NativeReservations
from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestNativeHooks(unittest.TestCase):
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
        source = (
            Path(__file__).resolve().parents[3]
            / "python/sglang/srt/mem_cache/allocation.py"
        )
        function = next(
            n
            for n in ast.parse(source.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "alloc_for_decode"
        )
        module = ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__", names=[ast.alias(name="annotations")], level=0
                ),
                function,
            ],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        self.assertEqual(namespace["alloc_for_decode"](batch, 1).tolist(), [8320])
        self.assertEqual(req.kv.kv_allocated_len, 4097)
        self.assertEqual(pages.available_size(), 0)
        native.release(req)
        self.assertEqual(pages.available_size(), 33 * 128)
        self.assertEqual(rows.available_size(), 1)
        self.assertIsNone(req.kv.req_pool_idx)
