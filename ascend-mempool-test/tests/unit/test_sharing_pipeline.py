"""Execute the real pipeline submission order over deterministic stream queues."""

import unittest
from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

from ascend_mempool.sharing_pipeline import Pipeline


class Event:
    def __init__(self):
        self.ready = False

    def reset(self, stream):
        stream.queue.append(("reset", self))


class Stream:
    def __init__(self, name):
        self.name = name
        self.queue = deque()

    def record_event(self, event):
        self.queue.append(("record", event))

    def wait_event(self, event):
        self.queue.append(("wait", event))


class Device:
    def __init__(self, streams):
        self.streams = streams
        self.current = streams[0]

    Event = Event

    @contextmanager
    def stream(self, stream):
        previous, self.current = self.current, stream
        try:
            yield
        finally:
            self.current = previous

    def synchronize(self):
        # An unresolved dependency fails immediately instead of a hardware hang.
        while any(stream.queue for stream in self.streams):
            progressed = False
            for stream in self.streams:
                if not stream.queue:
                    continue
                operation, event = stream.queue[0]
                if operation == "wait" and not event.ready:
                    continue
                stream.queue.popleft()
                if operation != "wait":
                    event.ready = operation == "record"
                progressed = True
            if not progressed:
                pending = ", ".join(s.name for s in self.streams if s.queue)
                raise RuntimeError(f"device synchronize deadlocked: {pending}")


class TestPipelineSubmission(unittest.TestCase):
    def test_delayed_writer_does_not_hold_back_selected_host_submission(self):
        # Model an eager host whose delay submissions cost as long as the device
        # kernels. The old order consumes the entire delay before selected exists.
        clock = [0.0]
        stamps = {}
        streams = [Mock() for _ in range(4)]
        for stream in streams:
            stream.record_event.side_effect = lambda event: stamps.__setitem__(
                event, clock[0]
            )
        npu = Mock()
        npu.stream.side_effect = lambda stream: Device(streams).stream(stream)
        torch = Mock(npu=npu)
        torch.mm.side_effect = lambda *a, **kw: clock.__setitem__(0, clock[0] + 1.0)
        work = Mock()
        work.case.operation = "current_parallel"
        work.pool.member = 1
        work.pool.layout.layout_for_rank.return_value = SimpleNamespace(
            slots=16, tokens=8192, row_bytes=576
        )
        work.history.side_effect = lambda: clock.__setitem__(0, clock[0] + 0.23)
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.torch, pipeline.work = torch, work
        pipeline.main, pipeline.writer, pipeline.history, pipeline.current = streams
        pipeline.materialization = None
        pipeline.selected_signal = None
        pipeline.publication = True
        pipeline.delay_matmuls = 512
        for name in (
            "ready",
            "writer_done",
            "history_done",
            "current_done",
            "selected_ready",
            "delay_source",
            "delay_target",
            "collective_value",
            "published",
            "publish_src",
            "publish_dst",
            "publish_valid",
        ):
            setattr(pipeline, name, Mock())
        pipeline.launch()
        self.assertGreater(
            stamps[pipeline.writer_done] - stamps[pipeline.selected_ready], 0
        )
        # Publication still follows the writer join; no selected->writer fence.
        pipeline.writer.wait_event.assert_called_once_with(pipeline.ready)
        pipeline.main.wait_event.assert_any_call(pipeline.writer_done)
        torch.distributed.all_reduce.assert_called_once()
        work.kernel.assert_called_once()
        # Non-publication calls retain the old submission order, so existing
        # benchmark measurements remain comparable across this diagnostic fix.
        pipeline.publication = False
        stamps.clear()
        pipeline.launch()
        self.assertLess(stamps[pipeline.writer_done], stamps[pipeline.selected_ready])

    def exercise(self, *, alias, timed):
        main, observer = Stream("main"), Stream("observer")
        communication = observer if alias else Stream("hccl")
        device = Device(list(dict.fromkeys((main, observer, communication))))
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.torch = SimpleNamespace(npu=device)
        pipeline.work = SimpleNamespace(side_active=True)
        pipeline.main, pipeline.observer = main, observer
        pipeline.selected_signal = Event()
        pipeline.materialization = None
        pipeline.observer_graph = SimpleNamespace(
            replay=lambda: observer.wait_event(pipeline.selected_signal)
        )

        def write_collective_read():
            # ProcessGroupHCCL orders its cached stream after the caller, then
            # makes the caller wait for communication before reading current KV.
            written, communicated = Event(), Event()
            main.record_event(written)
            communication.wait_event(written)
            communication.record_event(communicated)
            main.wait_event(communicated)
            main.record_event(pipeline.selected_signal)

        batches = []
        for _ in range(3):
            timers = (Event(), Event(), Event()) if timed else None
            batches.append(timers)
            pipeline.run(write_collective_read, timers)
        device.synchronize()
        for timers in batches:
            if timers:
                self.assertTrue(all(event.ready for event in timers))

    def test_cached_hccl_stream_can_alias_observer_after_pool_wraparound(self):
        for timed in (False, True):
            with self.subTest(timed=timed):
                self.exercise(alias=True, timed=timed)

    def test_distinct_communication_stream_also_drains(self):
        self.exercise(alias=False, timed=True)


if __name__ == "__main__":
    unittest.main()
