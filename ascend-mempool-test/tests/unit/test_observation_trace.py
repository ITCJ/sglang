"""Replay the user's 31 trace format without NPU or model initialization."""

import json
import tempfile
import unittest
from pathlib import Path

from analyze_k2_observation import step_timing, trace_ranges

# Device events transcribed from k2-ticket13-001/graph-prefill, 2026-11-03.
USER_RANGES = [
    ("cache_manage:0", 12, "1793717160578677.521", 1952.699),
    ("miss_io:0", 11, "1793717160579117.670", 1920.958),
    ("materialize_wait:0", 12, "1793717160579087.869", 1975.26),
    ("attention:0", 12, "1793717160579567.559", 1865.857),
    ("forward:1", 44, "1793717160577344.535", 2632.252),
    ("forward:2", 44, "1793717160579992.767", 1453.31),
]


def events_for(ranges):
    events = [
        dict(ph="M", name="thread_name", pid=2, tid=s, args=dict(name=f"Stream {s}"))
        for s in sorted({r[1] for r in ranges})
    ]
    events.extend(
        dict(
            name="mempool:" + name,
            ph="X",
            pid=2,
            tid=stream,
            ts=ts,
            dur=duration,
            args={"Physic Stream Id": stream, "Task Id": i},
        )
        for i, (name, stream, ts, duration) in enumerate(ranges)
    )
    return events


class TestObservationTrace(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "trace_view.json"

    def write(self, events):
        self.path.write_text(json.dumps(dict(traceEvents=events)))

    def test_deployed_stream_lanes_and_decimal_timestamps(self):
        events = events_for(USER_RANGES)
        events.extend(
            [
                dict(
                    ph="M", name="thread_name", pid=1, tid=7, args=dict(name="Thread 7")
                ),
                dict(name="mempool:forward:1", ph="X", pid=1, tid=7, ts="1.001", dur=2),
                dict(
                    ph="M",
                    name="thread_name",
                    pid=2,
                    tid=8,
                    args=dict(name="Stream 8 MSTX domain 0"),
                ),
                dict(name="mempool:legacy", ph="X", pid=2, tid=8, ts=1, dur="0.125"),
                dict(name="HcomAllReduce", ph="X", pid=2, tid=44, ts="1.001", dur=2),
                # A bare stream label without MSTX/task evidence is insufficient.
                dict(name="mempool:unidentified", ph="X", pid=2, tid=44, ts=1, dur=2),
            ]
        )
        self.write(events)
        spans = trace_ranges(self.path)
        forward = [r for r in spans if r["name"].startswith("forward:")]
        self.assertEqual(len(forward), 2)
        self.assertEqual(forward[0]["start_ns"], 1793717160577344535)
        self.assertEqual(forward[0]["end_ns"], 1793717160579976787)
        self.assertEqual(len(spans), 8)
        self.assertEqual(
            next(r for r in spans if r["name"] == "legacy")["end_ns"], 1125
        )

    def test_string_clock_anchor_aligns_with_device_time(self):
        self.write([dict(name="mempool:clock:1:1000", ph="X", ts="0.100", dur=0.001)])
        common = dict(
            host="d",
            role="decode",
            pid=7,
            rank=0,
            run_id="r",
            clock_domain="host_monotonic:d",
        )
        data = [
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
        ]
        device = [dict(trace=str(self.path), forward_ids="1", intervals=[])]
        row = step_timing(data, [self.path], device)[0]
        self.assertEqual(row["t_step_ns"], 150)
        self.assertEqual(row["anchor_uncertainty_ns"], 4)

    def test_user_merged_ranges_remain_failed_with_specific_evidence(self):
        from verify_observation_graph import verify_traces

        self.write(events_for(USER_RANGES))
        report = verify_traces(self.root)
        self.assertEqual(report["forward_counts"], {"forward:1": 1, "forward:2": 1})
        self.assertEqual(report["status"], "failed")
        self.assertIn("missing or merged", report["error"])
        self.assertEqual(len(report["replay_checks"]), 2)
        self.assertTrue(all(r["missing"] for r in report["replay_checks"]))
        self.assertEqual(len(report["device_ranges"]), 6)

    def test_complete_replays_pass_but_duplicate_identity_does_not(self):
        from verify_observation_graph import verify_traces

        ranges = []
        for i in (1, 2):
            ranges.append((f"forward:{i}", 44, i * 100, 90))
            for j, phase in enumerate(
                ("cache_manage", "miss_io", "materialize_wait", "attention")
            ):
                ranges.append(
                    (
                        f"{phase}:0",
                        43 if phase == "miss_io" else 44,
                        i * 100 + j * 20,
                        10,
                    )
                )
        self.write(events_for(ranges))
        self.assertEqual(verify_traces(self.root)["status"], "passed")
        ranges[5] = ("forward:1", 44, 200, 90)
        self.write(events_for(ranges))
        report = verify_traces(self.root)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["forward_counts"], {"forward:1": 2, "forward:2": 0})
        self.assertIn("device replay ranges", report["error"])
