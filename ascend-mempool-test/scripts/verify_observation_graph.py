"""One NPU command: capture once, replay twice, verify page snapshots and device ranges."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analyze_k2_observation import trace_ranges


def verify_traces(profile: Path) -> dict[str, Any]:
    """Check exported device evidence, also usable without importing torch/NPU."""
    paths = sorted(profile.rglob("trace_view.json"))
    spans = [s for path in paths for s in trace_ranges(path)]
    names = ("forward:1", "forward:2")
    outer = [s for s in spans if s["name"] in names]
    counts = {name: sum(s["name"] == name for s in outer) for name in names}
    report: dict[str, Any] = dict(
        status="failed",
        coverage="exported trace only; page snapshots not checked",
        traces=[str(p) for p in paths],
        forward_counts=counts,
        device_ranges=spans,
        replay_checks=[],
    )
    if any(count != 1 for count in counts.values()):
        report["error"] = (
            f"expected one each of two device replay ranges; found {counts}; "
            "inspect traces and device_ranges before attributing to MSTX/msprof"
        )
        return report
    required = {"cache_manage:0", "miss_io:0", "materialize_wait:0", "attention:0"}
    for forward in outer:
        nested = {
            s["name"]
            for s in spans
            if s["trace"] == forward["trace"]
            and forward["start_ns"] <= s["start_ns"] < s["end_ns"] <= forward["end_ns"]
        }
        report["replay_checks"].append(
            dict(forward=forward["name"], missing=sorted(required - nested))
        )
    if any(check["missing"] for check in report["replay_checks"]):
        report["error"] = (
            "captured layer/side-stream ranges missing or merged across replays; "
            "see replay_checks and device_ranges"
        )
    else:
        report["status"] = "passed"
    return report


def run(root: Path, device: int) -> dict[str, Any]:
    import torch
    import torch_npu  # noqa: F401

    from ascend_mempool.observation_device import (
        DeviceTrace,
        PageObservation,
        device_range,
    )

    torch.npu.set_device(device)
    trace = DeviceTrace(root / "profile")
    positions = torch.tensor([[0, 1, 4, 6], [-1, -1, -1, -1]], device="npu")
    rows = torch.tensor([1, 0], device="npu")
    prompt_lengths = torch.tensor([0, 4, 0], device="npu")
    hits = torch.tensor([[True, False, False, False], [False] * 4], device="npu")
    pages = PageObservation(2, 3, 8, 4, 4, (0,), 0, "npu")
    stream, miss = torch.npu.Stream(), torch.npu.Stream()
    ready, done = torch.npu.Event(), torch.npu.Event()
    graph = torch.npu.NPUGraph()

    def work() -> None:
        pages.reset()
        with device_range("cache_manage:0"):
            pages.cache(0, rows, positions, positions >= 0, hits)
        ready.record()
        with torch.npu.stream(miss):
            miss.wait_event(ready)
            with device_range("miss_io:0"):
                pages.io(0, rows, positions, (positions >= 0) & ~hits, prompt_lengths)
            done.record()
        with device_range("materialize_wait:0"):
            stream.wait_event(done)
        with device_range("attention:0"):
            pages.cache(1, rows, positions, positions >= 0, hits)

    try:
        stream.wait_stream(torch.npu.current_stream())
        with torch.npu.stream(stream):
            work()
        stream.synchronize()
        with torch.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
            work()
        snapshots = []
        with torch.npu.stream(stream):
            for forward in (1, 2):
                if forward == 2:
                    positions.fill_(-1)
                with device_range(f"forward:{forward}"):
                    graph.replay()
                snapshots.append(pages.snapshot())
        stream.synchronize()  # standalone gate, outside the measured graph
        counts = [item[0].cpu().tolist() for item in snapshots]
        if counts[0][0][1] != [[2, 1, 1, 1, 0, 0], [2, 0, 2, 0, 2, 0]]:
            raise ValueError("first replay page counts differ")
        if any(
            value
            for layer in counts[1]
            for row in layer
            for page in row
            for value in page
        ):
            raise ValueError("second replay retained stale selected rows")
    finally:
        trace.stop()
    report = verify_traces(root / "profile")
    report.update(
        coverage="two replays, distinct device ranges, side stream, snapshot reuse and padding; no model or BM contents",
        torch_version=torch.__version__,
        n=2,
        warmup=1,
        page_snapshots_status="passed",
        snapshots=counts,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="recheck existing profile traces without initializing NPU or overwriting the capture report",
    )
    args = parser.parse_args()
    if not args.log_dir.resolve().is_relative_to(Path("/home/cryang/log").resolve()):
        parser.error("logs must remain under /home/cryang/log/")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = (
            verify_traces(args.log_dir / "profile")
            if args.analyze_only
            else run(args.log_dir, args.device)
        )
    except Exception as exc:
        report = dict(status="failed", error=f"{type(exc).__name__}: {exc}")
    name = (
        "graph-trace-analysis.json" if args.analyze_only else "graph-observation.json"
    )
    (args.log_dir / name).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
