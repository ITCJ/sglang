"""One NPU command: capture once, replay twice, verify page snapshots and device ranges."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analyze_k2_observation import trace_ranges

from ascend_mempool.observation_device import DeviceTrace, PageObservation, device_range


def run(root: Path, device: int) -> dict:
    import torch
    import torch_npu  # noqa: F401

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
    paths = list((root / "profile").rglob("trace_view.json"))
    spans = [s for path in paths for s in trace_ranges(path)]
    outer = [s for s in spans if s["name"] in ("forward:1", "forward:2")]
    if len(outer) != 2:
        raise ValueError(
            "two distinct device replay ranges missing; check deployed MSTX/msprof"
        )
    for forward in outer:
        nested = {
            s["name"]
            for s in spans
            if s["trace"] == forward["trace"]
            and forward["start_ns"] <= s["start_ns"] < s["end_ns"] <= forward["end_ns"]
        }
        if not {
            "cache_manage:0",
            "miss_io:0",
            "materialize_wait:0",
            "attention:0",
        }.issubset(nested):
            raise ValueError(
                "captured layer/side-stream ranges missing or merged across replays"
            )
    return dict(
        status="passed",
        coverage="two replays, distinct device ranges, side stream, snapshot reuse and padding; no model or BM contents",
        torch_version=torch.__version__,
        n=2,
        warmup=1,
        snapshots=counts,
        traces=[str(p) for p in paths],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--device", type=int, default=0)
    args = parser.parse_args()
    if not args.log_dir.resolve().is_relative_to(Path("/home/cryang/log").resolve()):
        parser.error("logs must remain under /home/cryang/log/")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = run(args.log_dir, args.device)
    except Exception as exc:
        report = dict(status="failed", error=f"{type(exc).__name__}: {exc}")
    (args.log_dir / "graph-observation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
