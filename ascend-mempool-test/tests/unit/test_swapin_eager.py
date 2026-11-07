"""CPU contracts for the standalone eager experiment, not NPU performance."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from run_swapin_eager import describe, parse_args, snapshot
from swapin_worker import Timing, baseline_module
from ascend_mempool.swapin_experiment import selection, source_counts, summarize


class TestSwapinEager(unittest.TestCase):
    def test_topologies_and_memory_plan(self):
        for tp, groups in ((1, 1), (2, 1), (16, 8)):
            args = parse_args(
                ["--role", "prefill", "--tp-size", str(tp), "--run-id", "unit"]
            )
            plan = describe(args)
            self.assertEqual(plan["batches"], [1, 16])
            self.assertEqual(len(plan["numa_nodes"]), groups)
            self.assertEqual(
                plan["bytes_per_host"],
                [(11 if tp == 1 else 12) * 2**30 * groups, 2**30 * groups],
            )
            self.assertEqual(plan["iterations"], 100)
            self.assertFalse(plan["graph"] or plan["observe"] or plan["trace"])

    def test_selection_has_exact_counts_unique_positions_and_changes(self):
        counts = source_counts(2048)
        self.assertEqual(
            counts, dict(HBM_cache=1657, P_alive=385, D_alive=5, HBM_current=1)
        )
        for batch in (1, 16):
            first = selection(batch, 0)
            self.assertNotEqual(first["positions"], selection(batch, 1)["positions"])
            self.assertEqual(first, selection(batch, 0))
            for row, sources in zip(first["positions"], first["sources"]):
                self.assertEqual(len(set(row)), 2048)
                self.assertEqual({k: sources.count(k) for k in counts}, counts)
                self.assertEqual(row.count(8703), 1)

    def test_reports_do_not_mix_batch_sizes_or_sum_rank_latencies(self):
        ranks = []
        for rank in (0, 1):
            samples = []
            for batch in (1, 16):
                for variant in ("baseline", "materialize"):
                    for iteration in range(100):
                        samples.append(
                            dict(
                                batch=batch,
                                variant=variant,
                                iteration=iteration,
                                start_ns=rank * 1000000,
                                end_ns=(rank + batch) * 1000000,
                                total_ms=batch,
                                metadata_host_ms=1,
                                metadata_device_span_ms=2,
                                kernel_sum_ms=3,
                                copy_envelope_ms=4,
                                kernels_ms={"copy": 3},
                            )
                        )
            ranks.append(dict(rank=rank, samples=samples))
        report = summarize(ranks)
        self.assertEqual(report["baseline/b1"]["group_elapsed_ms"]["mean"], 2)
        self.assertEqual(report["materialize/b16"]["group_elapsed_ms"]["mean"], 17)
        ranks[0]["samples"].pop()
        with self.assertRaises(ValueError):
            summarize(ranks)

    def test_snapshot_failure_still_writes_report(self):
        import json
        from unittest.mock import patch
        from run_swapin_eager import run

        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args(
                [
                    "--role",
                    "prefill",
                    "--tp-size",
                    "1",
                    "--run-id",
                    "failure",
                    "--log-root",
                    tmp,
                ]
            )
            with patch(
                "run_swapin_eager.snapshot", side_effect=ValueError("baseline missing")
            ):
                with self.assertRaisesRegex(ValueError, "baseline missing"):
                    run(args)
            report = json.loads((Path(tmp) / "failure/prefill/report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["cleanup"], "not-started")
            self.assertIn("baseline missing", report["error"])

    def test_timing_metadata_excludes_copy_intervals(self):
        class Event:
            def record(self):
                self.stamp = clock[0]

            def elapsed_time(self, other):
                return other.stamp - self.stamp

        clock = [0.0]
        t = SimpleNamespace(npu=SimpleNamespace(Event=lambda **_: Event()))
        timer = Timing(t)
        timer.begin("miss")
        origin = timer.cursor["miss"][0]
        clock[0] += 2
        timer.call("miss", "P", lambda: clock.__setitem__(0, clock[0] + 5))
        clock[0] += 3
        timer.call("miss", "D", lambda: clock.__setitem__(0, clock[0] + 7))
        clock[0] += 1
        timer.finish("miss")
        result = timer.result(origin)
        self.assertEqual(result["metadata_device_span_ms"], 6)
        self.assertEqual(result["kernel_sum_ms"], 12)
        self.assertEqual(result["copy_envelope_ms"], 15)
        self.assertEqual(result["kernels_ms"], dict(P=5, D=7))

    def test_real_baseline_and_fused_fetch_match_all_four_sources(self):
        import torch
        from test_materialize import TestMaterialize
        from ascend_mempool.decode_write import DecodeKV, DecodeWriter
        from ascend_mempool.runtime import MempoolRuntime

        fixture = TestMaterialize()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        h = fixture.helper
        rt = h.runtime
        rt._decode = DecodeKV(rt.manager, DecodeWriter(0, 1), 9, "cpu")
        rt.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        h.p[2, 0] = 999
        h.p[2, 1] = 102
        h.d[3, 0] = 201
        initial = h.start()
        initial_positions = torch.tensor([[4, -1, -1, -1], [-1] * 4])
        initial_valid = rt.selected_kv_valid(
            5, initial.req_pool_indices, initial_positions
        )
        rt.fetch_selected_kv(
            5,
            initial.req_pool_indices,
            initial_positions,
            initial_valid,
            torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16),
        )
        rt.end_forward()
        h.events[-1].done = True
        rt.poll_completed()
        batch = h.start(offset=1)
        positions = torch.tensor([[0, 1, 4, 5], [-1] * 4])
        valid = rt.selected_kv_valid(5, batch.req_pool_indices, positions)
        hit = torch.zeros_like(valid)
        hit[0, 0] = True
        miss = valid & ~hit
        cache = torch.full((9, 4, 1, 4), 111, dtype=torch.bfloat16)
        hitrows = torch.tensor([4, 0, 0, 0, 0, 0, 0, 0])
        fused_fetch = rt._fetch
        calls = []

        def native(src, dst, si, di, mask, sc, dc, width, tasks, blocks, sp, dp):
            calls.append(blocks)
            source = h.sources[sp] if sp is not None else src
            dst.reshape(-1, 4)[di[mask]] = source.reshape(-1, 4)[si[mask]]

        rt._decode.kernel = native
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = snapshot(Path(tmp))
            oldcopy = baseline_module(directory, "copy")
            oldruntime = baseline_module(directory, "runtime")
            rt._fetch = oldcopy.KVFetch(rt.manager, 48, native)
            expected = torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16)
            native(
                cache,
                expected,
                hitrows,
                torch.arange(8),
                hit.flatten(),
                36,
                8,
                8,
                8,
                24,
                None,
                None,
            )
            oldruntime.MempoolRuntime.fetch_selected_kv(
                rt, 5, batch.req_pool_indices, positions, miss, expected
            )
            self.assertEqual(calls, [24, 48, 48, 48])
            rt._active.fetch_layers.clear()
            rt._fetch = fused_fetch
            actual = torch.zeros_like(expected)
            MempoolRuntime.fetch_selected_kv(
                rt,
                5,
                batch.req_pool_indices,
                positions,
                miss,
                actual,
                cache_buffer=cache,
                cache_rows=hitrows,
                cache_valid=hit.flatten(),
            )
            self.assertEqual(expected[0, :, 0, 0].tolist(), [111, 102, 201, 202])
            torch.testing.assert_close(actual, expected)
            # Metadata changes must affect the next call, including the cache mask.
            rt._active.fetch_layers.clear()
            positions[0, 1] = 0
            MempoolRuntime.fetch_selected_kv(
                rt,
                5,
                batch.req_pool_indices,
                positions,
                miss,
                actual,
                cache_buffer=cache,
                cache_rows=hitrows,
                cache_valid=hit.flatten(),
            )
            self.assertEqual(actual[0, 1, 0, 0].item(), h.p[2, 0, 0, 0].item())


if __name__ == "__main__":
    unittest.main()
