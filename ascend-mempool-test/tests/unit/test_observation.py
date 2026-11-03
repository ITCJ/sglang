"""Observation contracts, checked through the same interfaces used by the service."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ascend_mempool.observation import Observation, create_observation


class TestObservation(unittest.TestCase):
    def test_trace_disabled_manifest_preserves_non_trace_checks(self):
        from analyze_k2_observation import summarize
        from ascend_mempool_pd.mempool_router_protocol import RouterConfig

        self.assertFalse(RouterConfig("p", "d", "/model").observe_trace)
        common = dict(run_id="r", host="p", role="prefill", rank=0, pid=1, mono_ns=1, clock_domain="host:p")
        data = [
            dict(
                common,
                event="run",
                trace_enabled=False,
                tp_size=1,
                layers=1,
                producer_layers=[],
                sample_every=1,
            ),
            dict(
                common, event="forward_submitted", rid="q", forward_id=1, mode="prefill"
            ),
        ]
        report = summarize(data, [])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["coverage"]["device_timing"], "not-collected")
        data[0]["trace_enabled"] = True
        self.assertEqual(summarize(data, [])["status"], "incomplete")
        data[0]["trace_enabled"] = False
        data[0]["tp_size"] = 2
        self.assertEqual(summarize(data, [])["status"], "incomplete")

    def test_disabled_does_not_create_files_and_enabled_preserves_clock_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "evidence"
            self.assertIsNone(
                create_observation(
                    SimpleNamespace(observe=False), "run", "decode", 3, root=root
                )
            )
            self.assertFalse(root.exists())
            observer = Observation(root, "run", "decode", 3, clock=lambda: 17)
            observer.emit("sdk_complete", rid="r", op_id=2, nbytes=1152)
            observer.flush()
            record = json.loads(observer.path.read_text())
            self.assertEqual(record["mono_ns"], 17)
            self.assertEqual(record["rank"], 3)
            self.assertEqual(record["nbytes"], 1152)
            self.assertIn("host_monotonic:", record["clock_domain"])
            observer.close()

    def test_page_counts_distinguish_partial_hit_current_supply_and_padding(self):
        import torch

        from ascend_mempool.observation_device import PageObservation

        pages = PageObservation(2, 3, 8, 4, 4, (5,), 5, "cpu")
        rows = torch.tensor([1, 0])
        positions = torch.tensor([[0, 1, 4, 6], [-1, -1, -1, -1]])
        valid = positions >= 0
        hits = torch.tensor([[True, False, False, False], [False] * 4])
        pages.cache(5, rows, positions, valid, hits)
        pages.io(
            5,
            rows,
            positions,
            torch.tensor([[False, True, True, False], [False] * 4]),
            torch.tensor([0, 4, 0]),
        )
        # Shared layer selects another token in the same page: page equality
        # cannot establish token-selection equality.
        changed = positions.clone()
        changed[0, 0] = 1
        pages.cache(6, rows, changed, valid, hits)
        counts, equal = pages.snapshot()
        pages.reset()
        self.assertEqual(
            counts[0, 1].tolist(), [[2, 1, 1, 1, 0, 0], [2, 0, 2, 0, 1, 0]]
        )
        self.assertEqual(int(equal[1, 1]), 0)
        self.assertEqual(int(counts[:, 0].sum()), 0)
        self.assertEqual(int(pages.counts.sum()), 0)

    def test_unaligned_prompt_retains_both_physical_decode_pages(self):
        import torch

        from ascend_mempool.observation_device import PageObservation

        pages = PageObservation(1, 2, 12, 4, 2, (0,), 0, "cpu")
        positions = torch.tensor([[8, 9]])
        valid = torch.ones_like(positions, dtype=torch.bool)
        pages.io(0, torch.tensor([1]), positions, valid, torch.tensor([0, 5]))
        self.assertEqual(pages.counts[0, 1, 2].tolist(), [0, 0, 0, 0, 2, 1])


class TestOfflineObservation(unittest.TestCase):
    def test_page_analysis_uses_content_identity_and_actual_adjacent_steps(self):
        from analyze_k2_observation import (
            concentration,
            cross_step,
            fork_overlap,
            miss_rate,
        )

        def record(rid, step, pages):
            return dict(
                event="layer_pages",
                run_id="run",
                rid=rid,
                step=step,
                host="d",
                machine="d",
                role="decode",
                rank=0,
                attn_group=0,
                reader_numa=0,
                layer_id=2,
                page_size_tokens=128,
                hot_cache_tokens=2048,
                replacement_policy="replace_selected",
                page_hashes=["common", rid],
                selected_count=sum(p["selected_count"] for p in pages),
                miss_count=sum(p["miss_count"] for p in pages),
                io_miss_count=sum(p["io_miss_count"] for p in pages),
                pages=pages,
            )

        def page(content, selected, miss, io):
            return dict(
                content_page=content,
                selected_count=selected,
                hit_count=selected - miss,
                miss_count=miss,
                io_miss_count=io,
                current_device_supply_count=miss - io,
                sources=[dict(nbytes=io * 1152)],
            )

        data = [
            record("a", 1, [page("common", 4, 2, 1)]),
            record("a", 2, [page("common", 2, 1, 1), page("a", 2, 2, 2)]),
            record("a", 4, [page("a", 2, 0, 0)]),
            record("b", 1, [page("b", 2, 2, 2)]),
        ]
        rates = miss_rate(data)
        self.assertEqual(
            (rates[0]["selected"], rates[0]["cache_miss"], rates[0]["io_miss"]),
            (12, 7, 6),
        )
        self.assertEqual([r["overlap"] for r in cross_step(data)], [0.5])
        self.assertEqual(fork_overlap(data)[0]["jaccard"], 0.0)
        self.assertEqual(
            {r["unit"]: r["total"] for r in concentration(data)},
            {"rows": 6, "bytes": 6912},
        )
        data[0]["selected_count"] += 1
        with self.assertRaisesRegex(ValueError, "counts disagree"):
            miss_rate(data)

    def test_device_partition_subtracts_communication_and_overlapped_io(self):
        from analyze_k2_observation import device_steps

        def span(name, start, end, stream=1):
            return dict(name=name, start_ns=start, end_ns=end, stream=stream, trace="d")

        ranges = [
            span("forward:1", 0, 100),
            span("layer:model.layers.0", 0, 100),
            span("indexer:model.layers.0.indexer", 0, 20),
            span("collective:all_reduce", 5, 10),
            span("cache_manage:0", 20, 30),
            span("materialize_wait:0", 30, 60),
            span("miss_io:0", 10, 55, 2),
            span("miss_P_remote:0", 10, 45, 2),
            span("miss_D_local:0", 45, 55, 2),
            span("attention:0", 60, 85),
            span("moe:model.layers.0.mlp", 85, 100),
        ]
        step = device_steps(ranges)[0]
        self.assertEqual(sum(step["segments_ns"].values()), 100)
        self.assertEqual(
            step["segments_ns"],
            dict(
                indexer=15,
                collective_exposed=5,
                cache_manage=15,
                miss_exposed=25,
                attention=25,
                moe=15,
            ),
        )
        self.assertEqual(
            step["miss_exposed_by_source_ns"], dict(P_remote=15, D_local=10)
        )


class TestHostObservation(unittest.TestCase):
    def test_sdk_completion_is_distinct_from_tick_poll_and_empty_work(self):
        from ascend_mempool.pool_copy import (
            BMTransfer,
            CopyBlock,
            CopyTask,
            PoolCopyExecutor,
        )

        class Handle:
            def copy_data_batch(self, *args, **kwargs):
                return 0

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as folder:
            observer = Observation(Path(folder), "run", "prefill", 3)
            self.addCleanup(observer.close)
            transfer = BMTransfer(
                Handle(), SimpleNamespace(BmCopyType=SimpleNamespace(G2G=1))
            )
            transfer.observer = observer
            executor = PoolCopyExecutor(transfer, timeout=2)
            executor.observer = observer
            executor.submit(CopyTask("r", 1, "fill", (CopyBlock(1, 2, 1152),)))
            executor.submit(CopyTask("r", 2, "admit", ()))
            executor.close()
            self.assertEqual(executor.poll(1).state, "READY")
            self.assertEqual(executor.poll(2).state, "READY")
            observer.flush()
            rows = [json.loads(line) for line in observer.path.read_text().splitlines()]
            first = [r["event"] for r in rows if r["op_id"] == 1]
            self.assertLess(
                first.index("library_submit_return"), first.index("library_complete")
            )
            self.assertLess(
                first.index("library_complete"), first.index("copy_poll_observed")
            )
            empty = [r for r in rows if r["op_id"] == 2]
            self.assertFalse(any(r["event"] == "library_complete" for r in empty))
            self.assertTrue(
                next(r for r in empty if r["event"] == "executor_enqueue")["zero_work"]
            )

    def test_result_boundary_keeps_prebuilt_and_discarded_forward_separate(self):
        # The notification seam is called after the existing result processor.
        import test_pd_service

        helper = test_pd_service.TestMempoolPDService()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        service = helper.service
        with tempfile.TemporaryDirectory() as folder:
            observer = Observation(Path(folder), "run", "prefill", 0, clock=lambda: 100)
            self.addCleanup(observer.close)
            service.observer = observer
            req = helper.request(7, 1)
            req.output_ids = []
            req.finished_reason = "length"
            with patch(
                "ascend_mempool_pd.mempool_router_protocol.page_hashes", return_value=[]
            ):
                helper.bind_request(req, 0)
            identity = dict(rid=req.rid, forward_id=1, tick_ns=20, output_before=0)
            service.observation_batches[1] = [identity]
            service.observation_requests[req.rid]["prefill_forwards"] = 1
            batch = SimpleNamespace(
                forward_iter=1,
                reqs=[req],
                forward_mode=SimpleNamespace(is_prebuilt=lambda: False),
            )
            req.output_ids.append(11)
            service.observation_result(batch)
            service.observation_batches[2] = [dict(identity, forward_id=2)]
            batch.forward_iter = 2
            service.observation_result(
                batch
            )  # completed overlap forward whose output was dropped
            batch.forward_iter = 3
            batch.forward_mode.is_prebuilt = lambda: True
            service.observation_result(batch)
            observer.flush()
            rows = [json.loads(line) for line in observer.path.read_text().splitlines()]
            self.assertEqual(len([r for r in rows if r["event"] == "token_ready"]), 1)
            self.assertEqual(
                len([r for r in rows if r["event"] == "output_discarded"]), 1
            )
            state = service.observation_requests[req.rid]
            self.assertEqual(
                (
                    state["completed_forwards"],
                    state["discarded_forwards"],
                    state["prebuilt"],
                ),
                (2, 1, 1),
            )


class TestTraceContracts(unittest.TestCase):
    def test_hit_critical_wait_and_observer_work_are_excluded_from_miss_io(self):
        from analyze_k2_observation import device_steps, step_timing, summarize

        def span(name, a, b, stream=1):
            return dict(name=name, start_ns=a, end_ns=b, stream=stream, trace="trace")

        ranges = [
            span("forward:1", 100, 200),
            span("cache_wait:0", 110, 160),
            span("miss_P_remote:0", 110, 150, 2),
            span("materialize_wait:0", 160, 180),
            span("observer_io:0", 160, 170, 2),
            span("miss_D_local:0", 170, 180, 2),
        ]
        steps = device_steps(ranges)
        self.assertEqual(steps[0]["segments_ns"]["miss_exposed"], 10)
        self.assertEqual(steps[0]["segments_ns"]["observer_exposed"], 10)
        self.assertEqual(steps[0]["miss_exposed_by_source_ns"], {"D_local": 10})
        common = dict(
            run_id="r",
            host="d",
            machine="d",
            role="decode",
            rank=0,
            pid=7,
            clock_domain="host_monotonic:d",
            mono_ns=1000,
        )
        data = [
            dict(
                common,
                event="run",
                tp_size=2,
                layers=1,
                producer_layers=[0],
                sample_every=1,
            ),
            dict(
                common,
                event="clock_anchor",
                forward_ids="1",
                before_ns=1000,
                after_ns=1004,
            ),
            dict(
                common,
                event="token_ready",
                rid="q",
                forward_id=1,
                tick_id=1,
                tick_ns=1000,
                ready_ns=1150,
            ),
            dict(common, event="control_tick", tick_id=1, begin_ns=1000, end_ns=1020),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.json"
            path.write_text(
                json.dumps(
                    {
                        "traceEvents": [
                            dict(name="mempool:clock:1:1000", ph="X", ts=0.1, dur=0.001)
                        ]
                    }
                )
            )
            steps[0]["trace"] = str(path)
            row = step_timing(data, [path], steps)[0]
            self.assertEqual(row["t_step_ns"], 150)
            self.assertEqual(row["partition_error_ns"], 0)
            self.assertEqual(row["anchor_uncertainty_ns"], 4)
            report = summarize(data, [])
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(any("TP rank" in r for r in report["missing"]))


class TestDeliveryContracts(unittest.TestCase):
    def test_observation_bytes_match_functional_receipts_for_b_and_detect_missing_fill(
        self,
    ):
        from analyze_k2_observation import compare_copy_bytes

        functional = [
            dict(
                rid="B",
                prefix=dict(
                    load_back=dict(main_bytes=368050176, index_bytes=22020096),
                    fill=dict(main_bytes=2944401408, index_bytes=22020096),
                ),
            )
        ]
        actual = [
            dict(rid="B", kind="load_back", main_bytes=368050176, index_bytes=22020096),
            dict(rid="B", kind="fill", main_bytes=2944401408, index_bytes=22020096),
        ]
        self.assertTrue(
            all(r["passed"] for r in compare_copy_bytes(actual, functional))
        )
        self.assertFalse(compare_copy_bytes(actual[:1], functional)[1]["passed"])

    def test_directory_checks_all_rank_files_and_keeps_main_local(self):
        from analyze_k2_observation import summarize_directory

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            common = dict(
                schema_version=1,
                run_id="r",
                host="p",
                machine="p",
                pid=7,
                mono_ns=10,
                clock_domain="host_monotonic:p",
            )
            (root / "main.jsonl").write_text(
                json.dumps(
                    dict(
                        common,
                        role="main",
                        rank=0,
                        event="request_received",
                        rid="r1",
                        received_ns=9,
                    )
                )
                + "\n"
            )
            rows = [
                dict(
                    common,
                    role="prefill",
                    rank=0,
                    event="run",
                    tp_size=2,
                    layers=1,
                    producer_layers=[0],
                    sample_every=1,
                )
            ]
            (root / "p.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
            report = summarize_directory(root)
            self.assertEqual(report["status"], "incomplete")
            self.assertTrue(any("rank logs missing" in x for x in report["missing"]))
            self.assertEqual(len(list(root.glob("*-summary.json"))), 2)
            self.assertEqual(len(list(root.glob("*-summary.md"))), 2)

    def test_empty_replay_snapshot_uses_integer_rows(self):
        import test_runtime
        from ascend_mempool.observation_device import PageObservation

        helper = test_runtime.TestMempoolRuntime()
        self.addCleanup(helper.doCleanups)
        runtime, _, _ = helper.make_runtime(rank=1, layers=1)
        with tempfile.TemporaryDirectory() as folder:
            runtime.observer = Observation(Path(folder), "empty", "decode", 0)
            self.addCleanup(runtime.observer.close)
            runtime.observation_device = PageObservation(1, 9, 16, 4, 2, (5,), 5, "cpu")
            runtime._capture_validated = True
            runtime.begin_forward([], replay=True)
            runtime.end_forward()
            pages = runtime._pending[-1].observation["pages"]
            self.assertEqual(pages[0].shape[1], 0)
            self.assertEqual(pages[1].shape[1], 0)

    def test_collector_attributes_unaligned_locations_and_matches_existing_sources(
        self,
    ):
        import test_runtime
        import torch

        from ascend_mempool.observation import Observation
        from ascend_mempool.observation_device import PageObservation

        helper = test_runtime.TestMempoolRuntime()
        self.addCleanup(helper.doCleanups)
        runtime, _, _ = helper.make_runtime(rank=1, layers=1)
        with tempfile.TemporaryDirectory() as folder:
            observer = Observation(Path(folder), "r", "decode", 0)
            self.addCleanup(observer.close)
            runtime.observer = observer
            runtime.observation_page_size = 4
            runtime.observation_producers = (5,)
            runtime.observation_hashes["q"] = ["common"]
            device = PageObservation(1, 9, 16, 4, 2, (5,), 5, "cpu")
            positions = torch.tensor([[8, 9]])
            rows = torch.tensor([1])
            valid = torch.ones_like(positions, dtype=torch.bool)
            device.cache(5, rows, positions, valid, ~valid)
            device.io(
                5, rows, positions, valid, torch.tensor([0, 5, 0, 0, 0, 0, 0, 0, 0])
            )
            source = torch.zeros((1, 9, 2), dtype=torch.int64)
            source[0, 1, 1] = 2
            identity = dict(
                rid="q",
                step=1,
                forward_id=1,
                write_start=8,
                write_rows=1,
                prompt_tokens=5,
                prompt_slot=0,
                decode_slot=2,
                prefill_host="p",
                decode_host="d",
            )
            runtime.collect_observation(
                dict(
                    rows={1: identity}, pages=device.snapshot(rows), source_rows=source
                )
            )
            records = [json.loads(x) for x in observer.path.read_text().splitlines()]
            self.assertTrue(
                next(r for r in records if r["event"] == "read_byte_reconciliation")[
                    "passed"
                ]
            )
            pages = next(r for r in records if r["event"] == "layer_pages")["pages"]
            self.assertEqual([s["physical_page"] for s in pages[0]["sources"]], [4, 5])
            self.assertEqual(sum(s["rows"] for s in pages[0]["sources"]), 2)
