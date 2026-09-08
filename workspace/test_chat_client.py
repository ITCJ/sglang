"""Offline tests for response validation; no NPU or live server required."""
import io
import unittest

from test_chat import stream_result, validate


class ChatTests(unittest.TestCase):
    def test_stream(self):
        source = b': keepalive\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: {"choices":[{"delta":{"content":"2"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: {"choices":[],"usage":{"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        events = []
        content, reasoning, finish = stream_result(io.BytesIO(source), events)
        validate(content, finish)
        self.assertEqual(content, '2')
        self.assertEqual(len(events), 4)

    def test_truncated_stream(self):
        with self.assertRaisesRegex(RuntimeError, 'without'):
            stream_result(io.BytesIO(b'data: {"choices":[]}\n\n'), [])

    def test_error_event(self):
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            stream_result(io.BytesIO(b'data: {"error":"abort"}\n\n'), [])

    def test_empty_and_length_are_not_passes(self):
        for content, reason in [('', 'stop'), ('  ', 'stop'), ('partial', 'length'), ('x', None)]:
            with self.subTest(content=content, reason=reason), self.assertRaises(RuntimeError):
                validate(content, reason)


if __name__ == '__main__':
    unittest.main()
