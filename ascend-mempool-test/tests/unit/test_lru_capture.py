"""The component gate must capture on a non-default NPU stream."""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from verify_lru_cache import capture_step


class TestLRUCapture(unittest.TestCase):
    def test_capture_uses_side_stream_and_orders_input_and_completion(self):
        dependencies = []
        default = SimpleNamespace(
            wait_stream=lambda other: dependencies.append(("join", other))
        )
        side = SimpleNamespace(
            wait_stream=lambda other: dependencies.append(("input", other))
        )
        graph = object()
        calls = []

        @contextmanager
        def capture(actual_graph, *, stream, auto_dispatch_capture):
            if stream is default:
                raise RuntimeError(
                    "NPU graphs must be captured on a non-default stream"
                )
            self.assertIs(actual_graph, graph)
            self.assertEqual(dependencies, [("input", default)])
            yield

        npu = SimpleNamespace(
            current_stream=lambda: default,
            Stream=lambda: side,
            NPUGraph=lambda: graph,
            graph=capture,
        )
        self.assertIs(capture_step(SimpleNamespace(npu=npu), calls.append), graph)
        self.assertEqual(calls, [side])
        self.assertEqual(dependencies, [("input", default), ("join", side)])
