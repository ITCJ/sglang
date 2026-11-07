"""Whole swap-in graph timing, with changing inputs and no per-op probes."""

import time

from swapin_worker import Work


class GraphDispatch:
    """Keep only the fork/join dependency events; no timed copy brackets."""

    def __init__(self, torch):
        self.t = torch
        self.events = []

    def event(self):
        event = self.t.npu.Event()
        event.record()
        self.events.append(event)
        return event

    def begin(self, name):
        pass

    def call(self, stream, name, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def current_copy(self, fn, source, target, source_rows, *args):
        # Same baseline-only ABI normalization as the eager adapter.
        self.call(
            "miss", "current", fn, source, target, source_rows.contiguous(), *args
        )

    def finish(self, name):
        return self.event()


class GraphWork(Work):
    def __init__(self, *args):
        super().__init__(*args)
        self.main = self.t.npu.Stream()

    def capture(self, variant):
        t = self.t
        # Stable graph input addresses; update their contents before each replay.
        self.static, expected, _ = self.raw(0)
        with t.npu.stream(self.main):
            for _ in range(3):
                dispatch = GraphDispatch(t)
                self.execute(variant, self.static, dispatch).synchronize()
            self.dispatch = GraphDispatch(t)
            self.graph = t.npu.NPUGraph()
            with t.npu.graph(self.graph, stream=self.main, auto_dispatch_capture=True):
                self.execute(variant, self.static, self.dispatch)
            self.graph.replay()
        self.main.synchronize()
        if not t.equal(self.outputs[variant].cpu().squeeze(2), expected):
            raise AssertionError(f"graph warmup KV mismatch: {variant}")
        self.setup[variant]["graph"] = dict(
            captures=1, eager_warmups=3, untimed_replays=1, measured_replays=100
        )

    def run(self, variant, raw):
        t = self.t
        with t.npu.stream(self.main):
            for name, value in self.static.items():
                value.copy_(raw[name])
            self.outputs[variant].fill_(-99)
        self.main.synchronize()
        # Input staging and output poison are complete before either clock starts.
        begin = t.npu.Event(enable_timing=True)
        end = t.npu.Event(enable_timing=True)
        with t.npu.stream(self.main):
            start_ns = time.monotonic_ns()
            begin.record()
            self.graph.replay()
            end.record()
        end.synchronize()
        end_ns = time.monotonic_ns()
        return dict(
            start_ns=start_ns,
            end_ns=end_ns,
            total_ms=(end_ns - start_ns) / 1e6,
            graph_ms=begin.elapsed_time(end),
        )
