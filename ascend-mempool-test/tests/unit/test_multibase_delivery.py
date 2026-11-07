"""Check the measurement workload and cache evidence through CPU SDK boundaries."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import torch
from test_materialize import CPUStream
from test_multibase import cpu_multibase

from ascend_mempool.bandwidth import BenchmarkConfig, Case
from ascend_mempool.layout import KVLayout
from ascend_mempool.multibase_write import MultibaseWriteWork
from ascend_mempool.prefix_layout import PrefixLayout


class CPUTorch:
    def __getattr__(self, name):
        if name == "npu":
            return SimpleNamespace(
                Stream=CPUStream, Event=lambda: None, current_stream=CPUStream
            )
        value = getattr(torch, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            if kwargs.get("device") == "npu":
                kwargs["device"] = "cpu"
            return value(*args, **kwargs)

        return call


class TestMultibaseDelivery(unittest.TestCase):
    def test_graph_dummy_batch_reaches_runtime_write_and_stale_replay_guard(self):
        from test_runtime import TestMempoolRuntime

        from ascend_mempool.alive_pages import PageLease
        from ascend_mempool.multibase_graph import decode_capture_batch
        from ascend_mempool.runtime import KVWriteExpectation

        fixture = TestMempoolRuntime()
        self.addCleanup(fixture.doCleanups)
        runtime, targets, _ = fixture.make_runtime(rank=1, page_size=2)
        manager = runtime.manager
        manager.enable_page_tables("cpu")
        for side in (0, 1):
            manager.install_pages(side, 0, PageLease("graph-negative", 1, (0, 1)))
        batch = decode_capture_batch(torch.zeros(1, dtype=torch.int64))
        values = torch.zeros((1, 4), dtype=torch.bfloat16)
        # Exercise the same empty warmup/capture forwards as the NPU microtest,
        # keeping real row derivation, writes, counters and replay validation.
        for _ in range(2):
            runtime.begin_forward([], capture=True)
            for layer in range(5, 7):
                runtime.write_layer(layer, values[:, :2], values[:, 2:], batch)
            runtime.end_forward()
        self.assertTrue(all(torch.all(target == -1) for target in targets))
        self.assertEqual(torch.count_nonzero(runtime._write_counts).item(), 0)
        runtime.bind(1, slot=0, prompt_tokens=2, prompt_slot=0)
        manager.active_page_versions[1, 0] = 2
        with self.assertRaisesRegex(RuntimeError, "stale request page table"):
            runtime.begin_forward([KVWriteExpectation(1, 2, 1)], replay=True)

    def test_cache_gate_rejects_any_rank_error_even_when_another_rank_passes(self):
        import json
        import tempfile
        from pathlib import Path

        from run_ticket26 import cache_eviction

        record = dict(
            event="materialization",
            run_id="run",
            rid="long",
            rank=0,
            step=100,
            evicted_entries=2,
            refilled_rows=4,
            slot_map_updates=4,
            cache_refill_checked_rows=4,
            cache_refill_errors=0,
            slot_map_checked_rows=4,
            slot_map_missing=0,
            slot_map_stale=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rows.jsonl").write_text(
                json.dumps(record)
                + "\n"
                + json.dumps(dict(record, rank=1, slot_map_stale=1))
                + "\n"
            )
            self.assertEqual(cache_eviction(root)["status"], "failed")

    def test_sixteen_writers_create_eight_remote_replicas_and_one_candidate_copy(self):
        config = BenchmarkConfig(page_size=2, capacity=64, layers=1)
        prefix = PrefixLayout(32, tuple((2 * i, 2 * i + 1) for i in range(8)))
        arenas, views = {}, {}
        for side in (0, 1):
            for segment in range(8):
                for region in (
                    ("alive_main", "prefix_main", "prefix_index")
                    if side == 0
                    else ("alive_main",)
                ):
                    layout = KVLayout(
                        21 if region.endswith("index") else 1,
                        4 if region.startswith("prefix") else 2,
                        2 if region.startswith("prefix") else 64,
                        1,
                        128 if region.endswith("index") else 576,
                        page_size=2,
                    )
                    memory = torch.full(
                        (layout.total_bytes // 2,), -1, dtype=torch.bfloat16
                    )
                    arenas[side, segment, region] = memory
                    views[side, segment, region] = SimpleNamespace(
                        layout=layout, layer=0, device_base=memory.data_ptr()
                    )

        def view(side, segment, layer=0, region="alive_main"):
            return views[side, segment, region]

        real_to = torch.Tensor.to

        def cpu_to(tensor, *args, **kwargs):
            if args and args[0] == "npu":
                args = ("cpu", *args[1:])
            return real_to(tensor, *args, **kwargs)

        # Tensor.to and allocation devices are the unavailable device boundary.
        with (
            patch.object(torch.Tensor, "to", cpu_to),
            patch("ascend_mempool.multibase.torch", CPUTorch()),
            patch("ascend_mempool.multibase.load_kernel", return_value=cpu_multibase),
        ):
            work = []
            for rank in range(16):
                device = SimpleNamespace(
                    config=config,
                    torch=CPUTorch(),
                    rank=rank,
                    side=0,
                    pool=SimpleNamespace(session=SimpleNamespace(prefix_layout=prefix)),
                    view=view,
                )
                item = MultibaseWriteWork(
                    device,
                    Case(
                        "dual",
                        "remote_write",
                        operation="write",
                        targets="dual",
                        tokens=32,
                    ),
                    variant=1,
                )
                item.launch()
                work.append(item)
            self.assertEqual(
                sum(w.target_bytes["remote_main"] for w in work), 8 * 32 * 1152
            )
            self.assertEqual(sum(w.target_bytes["local_main"] for w in work), 32 * 1152)
            self.assertEqual(sum(w.target_bytes["local_index"] for w in work), 32 * 256)
            for segment in range(8):
                physical = arenas[1, segment, "alive_main"].view(64, 1, 2, 576)
                for token in range(32):
                    page, within = divmod(token, 2)
                    self.assertEqual(
                        physical[(page * 17) % 32, 0, within, 0].item(), token + 19
                    )
            for item in work:
                item.verify()


if __name__ == "__main__":
    unittest.main()
