"""Check the measurement workload and cache evidence through CPU SDK boundaries."""

import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import test_materialize as materialize_fixture
import torch
from test_materialize import CPUStream
from test_multibase import cpu_multibase

from ascend_mempool.bandwidth import BenchmarkConfig, Case
from ascend_mempool.layout import KVLayout
from ascend_mempool.multibase_write import MultibaseWriteWork
from ascend_mempool.observation_device import PageObservation
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
    def test_side_stream_observation_indices_survive_materialization_return(self):
        fixture = materialize_fixture.TestMaterialize()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        helper, cache = fixture.helper, fixture.cache
        helper.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        observer = PageObservation(1, 9, 24, 1, 4, (5,), 5, "cpu")
        helper.runtime.observation_device = observer
        helper.runtime._fetch.count_sources = True
        torch.npu.mstx = SimpleNamespace(
            range_start=lambda *args: 0, range_end=lambda *args: None
        )
        retained, reads = {}, []

        def record(stream, *tensors):
            # Model the allocator's ownership until the consumer completes.
            retained.setdefault(stream, []).extend(tensors)

        refill = observer.refill_verified
        lookup = fixture.module.slot_map_lookup

        def observe_refill(layer, rows, *args):
            reads.append((cache._materialize_refill_stream, weakref.ref(rows)))
            return refill(layer, rows, *args)

        lookup_calls = 0

        def observe_lookup(table, rows, positions):
            nonlocal lookup_calls
            lookup_calls += 1
            if lookup_calls == 2:
                reads.extend(
                    (cache._materialize_slot_map_stream, weakref.ref(value))
                    for value in (rows, positions)
                )
            return lookup(table, rows, positions)

        batch = helper.start(offset=0)
        helper.runtime.observer = object()
        with (
            patch.object(
                fixture.module, "_record_materialize_stream", record, create=True
            ),
            patch.object(observer, "refill_verified", observe_refill),
            patch.object(fixture.module, "slot_map_lookup", observe_lookup),
        ):
            cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                torch.tensor([[0, 1, -1, -1], [-1, -1, -1, -1]]),
                torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16),
                CPUStream(),
                mempool_runtime=helper.runtime,
            )
        # The caller has not yet joined refill/map. Locals may have gone out
        # of scope, but each consumer's allocator ownership must still exist.
        self.assertEqual(len(reads), 3)
        for stream, reference in reads:
            self.assertIsNotNone(
                reference(), "main-stream indices died before side-stream join"
            )
            self.assertTrue(any(value is reference() for value in retained[stream]))

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

    def test_observation_checks_actual_refill_and_new_map_after_eviction(self):
        fixture = materialize_fixture.TestMaterialize()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        helper, cache = fixture.helper, fixture.cache
        helper.runtime.bind(1, slot=3, prompt_tokens=4, prompt_slot=2)
        helper.p[2, :4] = torch.tensor([11, 12, 13, 14])[:, None, None]
        observer = PageObservation(1, 9, 24, 1, 4, (5,), 5, "cpu")
        helper.runtime.observation_device = observer
        helper.runtime._fetch.count_sources = True
        torch.npu.mstx = SimpleNamespace(
            range_start=lambda *args: 0, range_end=lambda *args: None
        )
        for step, chosen in enumerate(([0, 1, -1, -1], [2, 3, -1, -1])):
            batch = helper.start(offset=step)
            helper.runtime.observer = object()
            output = torch.zeros((2, 4, 1, 4), dtype=torch.bfloat16)
            cache.materialize_selected_kv(
                SimpleNamespace(layer_id=5),
                batch,
                torch.tensor([chosen, [-1] * 4]),
                output,
                CPUStream(),
                mempool_runtime=helper.runtime,
            )
            helper.runtime.observer = None
            helper.runtime.end_forward()
            helper.events[-1].done = True
            helper.runtime.poll_completed()
        _, changes, checked = observer.materialization_snapshot(torch.tensor([1]))
        self.assertEqual(changes[0, 0, 0].item(), 2)
        self.assertEqual(checked[0, 0].tolist(), [2, 0, 2, 0, 0])
        self.assertEqual(cache.device_slot_map[0][1, :4].tolist(), [-1, -1, 0, 1])
        self.assertEqual(cache.device_kv_buffer[0][1, :2, 0, 0].tolist(), [13, 14])


if __name__ == "__main__":
    unittest.main()
