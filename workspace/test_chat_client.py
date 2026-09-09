"""Offline tests for response validation; no NPU or live server required."""
import io
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import URLError, HTTPError

from test_chat import stream_result, validate, wait_for_health


class ChatTests(unittest.TestCase):
    def test_health_retry_then_ready(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch('test_chat.urllib.request.urlopen', side_effect=[URLError('refused'), response]) as opening, patch('test_chat.time.sleep') as sleep:
            wait_for_health('http://localhost', {}, 30)
        self.assertEqual(opening.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_health_deadline(self):
        with patch('test_chat.time.monotonic', side_effect=[0, 0, 2, 2]), patch('test_chat.time.sleep') as sleep, patch('test_chat.urllib.request.urlopen', side_effect=URLError('refused')) as opening:
            with self.assertRaisesRegex(TimeoutError, 'last error'):
                wait_for_health('http://localhost', {}, 2)
        self.assertEqual(opening.call_args.kwargs['timeout'], 2)
        sleep.assert_called_once_with(0.0)

    def test_health_auth_fails_without_retry(self):
        error = HTTPError('http://localhost', 401, 'Unauthorized', {}, None)
        with patch('test_chat.urllib.request.urlopen', side_effect=error), patch('test_chat.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'API_KEY'):
                wait_for_health('http://localhost', {}, 30)
        sleep.assert_not_called()

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
