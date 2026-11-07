"""Graph controller contracts on CPU; hardware capture remains user validation."""

import contextlib
import unittest
from types import SimpleNamespace

from run_swapin_eager import describe, parse_args
from swapin_graph import GraphDispatch, GraphWork
from ascend_mempool.swapin_experiment import summarize


class TestSwapinGraph(unittest.TestCase):
    def test_plan_and_graph_report_keep_only_whole_graph_timings(self):
        for tp in (1, 2, 16):
            plan = describe(
                parse_args(
                    [
                        "--role",
                        "decode",
                        "--tp-size",
                        str(tp),
                        "--run-id",
                        "unit",
                        "--graph",
                    ]
                )
            )
            self.assertTrue(plan["graph"])
            self.assertFalse(plan["observe"] or plan["trace"])
            self.assertEqual(plan["block_dims"]["multibase"], 48)
        ranks = []
        for rank in (0, 1):
            samples = [
                dict(
                    batch=b,
                    variant=v,
                    iteration=i,
                    graph_ms=0.2,
                    total_ms=0.3,
                    start_ns=rank * 100000,
                    end_ns=rank * 100000 + 300000,
                )
                for b in (1, 16)
                for v in ("baseline", "materialize")
                for i in range(100)
            ]
            ranks.append(dict(rank=rank, samples=samples))
        report = summarize(ranks, graph=True)
        self.assertEqual(len(report), 4)
        self.assertEqual(report["baseline/b1"]["group_elapsed_ms"]["mean"], 0.4)
        self.assertEqual(
            set(report["baseline/b1"]["per_rank"]["0"]), {"total_ms", "graph_ms"}
        )
        ranks[0]["samples"].pop()
        with self.assertRaises(ValueError):
            summarize(ranks, graph=True)

    def test_graph_replay_uses_updated_stable_inputs_and_excludes_staging(self):
        import torch

        clock, calls = [0.0], []

        class Stream:
            def synchronize(self):
                calls.append("synchronize")

        class Event:
            def record(self):
                calls.append("event")
                self.stamp = clock[0]

            def synchronize(self):
                calls.append("done")

            def elapsed_time(self, other):
                return other.stamp - self.stamp

        stream = Stream()
        npu = SimpleNamespace(
            stream=lambda _: contextlib.nullcontext(), Event=lambda **_: Event()
        )
        work = GraphWork.__new__(GraphWork)
        work.t, work.main = SimpleNamespace(npu=npu), stream
        work.static = dict(positions=torch.zeros(2), slot_map=torch.zeros(2))
        work.outputs = dict(baseline=torch.zeros(2))
        addresses = {k: v.data_ptr() for k, v in work.static.items()}

        def replay():
            calls.append("replay")
            self.assertTrue((work.outputs["baseline"] == -99).all())
            work.outputs["baseline"].copy_(
                work.static["positions"] + work.static["slot_map"]
            )
            clock[0] += 0.25

        work.graph = SimpleNamespace(replay=replay)
        for i in range(2):
            calls.clear()
            raw = dict(
                positions=torch.tensor([i, i + 1.0]), slot_map=torch.tensor([2.0, 3.0])
            )
            sample = work.run("baseline", raw)
            self.assertTrue(
                torch.equal(
                    work.outputs["baseline"], raw["positions"] + raw["slot_map"]
                )
            )
            self.assertEqual(
                addresses, {k: v.data_ptr() for k, v in work.static.items()}
            )
            self.assertEqual(sample["graph_ms"], 0.25)
            self.assertEqual(calls, ["synchronize", "event", "replay", "event", "done"])

    def test_graph_dispatch_has_dependencies_but_no_per_copy_events(self):
        import torch

        events, launches = [], []

        class Event:
            def record(self):
                events.append(self)

        dispatch = GraphDispatch(SimpleNamespace(npu=SimpleNamespace(Event=Event)))
        dispatch.begin("miss")
        index = torch.tensor([1]).expand(2048)
        dispatch.current_copy(
            lambda src, dst, indices: launches.append(indices.is_contiguous()),
            None,
            None,
            index,
        )
        self.assertEqual(launches, [True])
        self.assertEqual(events, [])
        dispatch.finish("miss")
        self.assertEqual(len(events), 1)
