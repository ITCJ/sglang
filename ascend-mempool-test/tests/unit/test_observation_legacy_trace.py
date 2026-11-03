"""CANN 9.0 regression from the user's two-replay NPU capture."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from analyze_k2_observation import device_steps, trace_ranges
from verify_observation_graph import verify_traces


class TestLegacyTrace(unittest.TestCase):
    fixture = Path(__file__).parents[1] / "fixtures/mstx-cann90/worker"

    def test_captured_hccl_tasks_keep_main_stream_attribution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "worker"
            shutil.copytree(self.fixture, root)
            path = root / "ASCEND_PROFILER_OUTPUT/trace_view.json"
            data = json.loads(path.read_text())
            event = next(
                e
                for e in data["traceEvents"]
                if e.get("name") == "mempool:attention:0" and e.get("tid") == 12
            )
            data["traceEvents"].append(dict(event, name="HcomAllReduce", dur=1))
            path.write_text(json.dumps(data))
            spans = trace_ranges(path)
            task = next(s for s in spans if s["name"] == "hccl_task:HcomAllReduce")
            self.assertEqual(task["logical_stream"], 44)
            self.assertEqual(
                device_steps(spans)[0]["segments_ns"]["collective_exposed"], 1000
            )

    def test_real_capture_recovers_distinct_replays_and_stream_roles(self):
        report = verify_traces(self.fixture)
        self.assertEqual(report["status"], "passed", report)
        spans = report["device_ranges"]
        cache = [s for s in spans if s["name"] == "cache_manage:0"]
        self.assertEqual(len(cache), 3)  # warmup plus two device executions
        replay = [s for s in cache if s["stream"] == 12]
        self.assertEqual(
            [s["end_ns"] - s["start_ns"] for s in replay], [396948, 362827]
        )
        self.assertTrue(all(s["logical_stream"] == 44 for s in replay))
        steps = device_steps(spans)
        self.assertEqual(len(steps), 2)
        self.assertTrue(all(s["segments_ns"]["attention"] > 350000 for s in steps))
        self.assertTrue(all(s["segments_ns"]["cache_manage"] > 300000 for s in steps))

    def test_missing_endpoint_is_rejected_instead_of_inventing_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "worker"
            shutil.copytree(self.fixture, root)
            raw = root / "PROF_fixture/device_0/data/ts_track.data.0.slice_0"
            data = raw.read_bytes()
            # Last record is forward:2's end; preserve every other raw record.
            raw.write_bytes(data[:-40])
            with self.assertRaisesRegex(ValueError, "unpaired.*10"):
                trace_ranges(root / "ASCEND_PROFILER_OUTPUT/trace_view.json")
