"""Run the real two-side benchmark protocol with an injected device boundary."""

import concurrent.futures
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cd_benchmark
import run_numa_cd as runner
from cd_results import local_groups

from ascend_mempool.cd_plan import topologies
from ascend_mempool.sharing_control import Cohort, CohortServer


class FakeGraph:
    def __init__(self):
        self.calls = []

    def replay(self):
        for call in self.calls:
            call()


class FakeEvent:
    def __init__(self, **kwargs):
        self.stamp = None

    def record(self, stream):
        self.stamp = time.monotonic_ns()

    def elapsed_time(self, other):
        return (other.stamp - self.stamp) / 1e6


class Device:
    local = threading.local()
    instances = []

    @contextmanager
    def graph(self, graph, **kwargs):
        self.local.capture = graph
        try:
            yield
        finally:
            self.local.capture = None

    def Stream(self):
        return SimpleNamespace(synchronize=lambda: None)

    def stream(self, stream):
        return nullcontext()

    def synchronize(self):
        return None

    Event = FakeEvent
    NPUGraph = FakeGraph


class Work:
    def __init__(self, pool, case, **kwargs):
        self.pool, self.case = pool, case
        self.variant = 0
        self.executed = None
        self.checked = []
        Device.instances.append(self)

    def prepare(self, variant):
        self.variant = variant

    def reset(self):
        self.executed = None

    def launch(self):
        graph = getattr(Device.local, "capture", None)
        if graph is None:
            self.copy()
        else:
            graph.calls.append(self.copy)

    def copy(self):
        self.executed = self.variant

    def verify(self):
        assert self.executed == self.variant, (
            "graph used stale contents or skipped a replay"
        )
        self.checked.append(self.variant)


class TestCDBenchmark(unittest.TestCase):
    def test_graph_updates_both_sides_checkpoint_and_resume_without_remeasuring(self):
        config = runner.parse_args(["--smoke", "--describe", "--read-sources", "local"])
        topology = topologies([0, 2, 4, 6])[0]
        Device.instances = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def run(run_id, resume=False):
                server = CohortServer("127.0.0.1", 0, run_id, 5, 1)
                port = server.server.server_address[1]

                def worker(side):
                    local_args = runner.parse_args(
                        [
                            "--smoke",
                            "--describe",
                            "--role",
                            ("prefill", "decode")[side],
                            "--read-sources",
                            "local",
                        ]
                    )
                    c = runner.contract(
                        local_args, dict(commit="stable"), topology, 0, 0, [[0]]
                    )
                    output = root / run_id / str(side)
                    output.mkdir(parents=True)
                    source = root / "first" / str(side)
                    reusable = (
                        [p.stem for p in (source / "cases").glob("*.json")]
                        if resume
                        else []
                    )
                    settings = dict(
                        cd_preflight=False,
                        cd_contract=c,
                        cd_reuse=reusable,
                        cd_resume_root=str(source),
                        cd_environment=dict(sdk="fake"),
                        cd_numa_verified=False,
                        smoke=True,
                        samples=config.samples,
                        repeats=config.repeats,
                        warmup=config.warmup,
                    )
                    pool = SimpleNamespace(
                        rank=side, member=0, mode="independent", tp_size=1
                    )
                    cohort = Cohort("127.0.0.1", port, side, run_id, 5, 1)
                    try:
                        rows = cd_benchmark.benchmark(
                            pool,
                            cohort,
                            settings,
                            SimpleNamespace(npu=Device()),
                            output,
                        )
                        cohort.exchange("drained", success=True, drained=True)
                        return rows
                    finally:
                        cohort.close()

                try:
                    with concurrent.futures.ThreadPoolExecutor(2) as threads:
                        return list(threads.map(worker, (0, 1)))
                finally:
                    server.close()

            with (
                patch.object(cd_benchmark, "CDWorkload", Work),
                patch("builtins.print"),
            ):
                first = run("first")
                before = len(
                    [w for w in Device.instances if w.case.name != "tail-2049"]
                )
                second = run("second", resume=True)
            self.assertEqual(first, second)
            self.assertEqual(len(first[0]), 4)  # two active cases x two samples
            self.assertEqual(before, 4)  # no workload constructed for the idle side
            measured = [w for w in Device.instances if w.case.name != "tail-2049"]
            self.assertEqual(
                len(measured), before
            )  # resume only repeats tail correctness
            self.assertTrue(all(w.checked == [0, 3] for w in measured))
            for side in (0, 1):
                checkpoints = list(
                    (root / "second" / str(side) / "cases").glob("*.json")
                )
                self.assertEqual(len(checkpoints), 4)
                for path in checkpoints:
                    record = json.loads(path.read_text())
                    self.assertIn("reused_from", record)
                    self.assertFalse(record["numa_verified"])
                    groups = local_groups([record])
                    self.assertEqual(
                        len(groups), 2 if record["consumer_side"] == side else 0
                    )


if __name__ == "__main__":
    unittest.main()
