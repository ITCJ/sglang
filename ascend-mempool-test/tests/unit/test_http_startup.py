"""Replay the skipped-warmup HTTP race without loading device-only imports."""

import ast
import io
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests


def startup_functions(post, *, skip=True, joiner=False):
    """Load both real startup functions; replace only runtime/HTTP boundaries."""
    source = (
        Path(__file__).resolve().parents[3]
        / "python/sglang/srt/entrypoints/http_server.py"
    )
    names = {"_wait_and_warmup", "_freeze_gc_after_server_warmup"}
    functions = [
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in functions} != names:
        raise AssertionError("production startup functions are missing")
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *functions,
        ],
        type_ignores=[],
    )
    clock = [0.0]
    output = io.StringIO()
    logger = logging.Logger("startup-regression")
    logger.addHandler(logging.StreamHandler(output))
    namespace = dict(
        requests=SimpleNamespace(post=post, exceptions=requests.exceptions),
        time=SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
        get_serving=lambda: SimpleNamespace(
            skip_server_warmup=skip, admin_api_key="admin-test", api_key="user-test"
        ),
        get_model=lambda: SimpleNamespace(
            checkpoint_engine_wait_weights_before_ready=False,
            delete_ckpt_after_loading=False,
        ),
        get_exec=lambda: SimpleNamespace(
            moe=SimpleNamespace(is_ep_scale_joiner=joiner, ep_join_mode="join")
        ),
        get_observability=lambda: SimpleNamespace(debug_tensor_dump_input_file=None),
        _global_state=SimpleNamespace(tokenizer_manager=SimpleNamespace()),
        ServerStatus=SimpleNamespace(Up="up"),
        _execute_server_warmup=Mock(return_value=True),
        ssl_verify_of=lambda args: "test-ca.pem",
        logger=logger,
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return SimpleNamespace(
        run=namespace["_wait_and_warmup"],
        warmup=namespace["_execute_server_warmup"],
        clock=clock,
        output=output,
    )


class TestHTTPStartup(unittest.TestCase):
    def test_warmup_modes_preserve_freeze_auth_tls_and_callback(self):
        for skip, joiner in ((True, False), (False, False), (False, True)):
            with self.subTest(skip=skip, joiner=joiner):
                response = Mock()
                post = Mock(return_value=response)
                startup = startup_functions(post, skip=skip, joiner=joiner)
                callback = Mock()
                args = SimpleNamespace(url=lambda: "https://localhost:8000")
                startup.run(args, launch_callback=callback)
                self.assertEqual(
                    startup.warmup.call_count, int(not skip and not joiner)
                )
                post.assert_called_once_with(
                    "https://localhost:8000/freeze_gc",
                    headers={"Authorization": "Bearer admin-test"},
                    timeout=10,
                    verify="test-ca.pem",
                )
                response.raise_for_status.assert_called_once()
                callback.assert_called_once()
                self.assertNotIn("Traceback", startup.output.getvalue())

    def test_persistent_connection_failure_is_bounded_and_still_reported(self):
        post = Mock(side_effect=requests.exceptions.ConnectionError("refused"))
        startup = startup_functions(post)
        startup.run(SimpleNamespace(url=lambda: "http://localhost:8000"))
        self.assertGreater(post.call_count, 1)
        self.assertLessEqual(startup.clock[0], 10)
        self.assertIn("post-warmup freeze_gc failed", startup.output.getvalue())
        self.assertIn("Traceback", startup.output.getvalue())

    def test_connection_attempt_time_counts_toward_retry_budget(self):
        timeouts = []

        def post(url, **kwargs):
            timeouts.append(kwargs["timeout"])
            startup.clock[0] += 6 if len(timeouts) == 1 else kwargs["timeout"]
            raise requests.exceptions.ConnectionError("still unavailable")

        startup = startup_functions(post)
        startup.run(SimpleNamespace(url=lambda: "http://localhost:8000"))
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0], 10)
        self.assertAlmostEqual(timeouts[1], 3.9)
        self.assertEqual(startup.clock[0], 10)
        self.assertIn("Traceback", startup.output.getvalue())

    def test_http_rejection_read_timeout_and_tls_error_are_not_retried(self):
        for error in (
            requests.exceptions.HTTPError("403 Forbidden"),
            requests.exceptions.ReadTimeout("freeze did not complete"),
            requests.exceptions.SSLError("certificate verification failed"),
        ):
            with self.subTest(error=type(error).__name__):
                response = Mock()
                post = Mock(return_value=response)
                if isinstance(error, requests.exceptions.HTTPError):
                    response.raise_for_status.side_effect = error
                else:
                    post.side_effect = error
                startup = startup_functions(post)
                startup.run(SimpleNamespace(url=lambda: "http://localhost:8000"))
                post.assert_called_once()
                self.assertIn("Traceback", startup.output.getvalue())

    def test_failed_warmup_does_not_freeze_or_call_launch_callback(self):
        post = Mock()
        startup = startup_functions(post, skip=False)
        startup.warmup.return_value = False
        callback = Mock()
        startup.run(SimpleNamespace(), launch_callback=callback)
        post.assert_not_called()
        callback.assert_not_called()

    def test_skipped_warmup_retries_until_http_listener_accepts_freeze(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        server.server_bind()
        self.addCleanup(server.server_close)
        serving = None
        attempts = []
        with requests.Session() as session:
            session.trust_env = False

            def post(url, **kwargs):
                nonlocal serving
                attempts.append(url)
                try:
                    return session.post(url, **kwargs)
                except requests.exceptions.ConnectionError:
                    # Bind before the warmup thread; listen only after its first
                    # connection is refused, as in the captured NPU startup log.
                    if serving is None:
                        server.server_activate()
                        serving = threading.Thread(
                            target=lambda: server.serve_forever(poll_interval=0.01)
                        )
                        serving.start()
                    raise

            startup = startup_functions(post)
            callback = Mock()
            try:
                startup.run(
                    SimpleNamespace(
                        url=lambda: f"http://127.0.0.1:{server.server_port}"
                    ),
                    launch_callback=callback,
                )
            finally:
                if serving is not None:
                    server.shutdown()
                    serving.join(timeout=2)
            self.assertEqual(received, [("/freeze_gc", "Bearer admin-test")])
            self.assertEqual(len(attempts), 2)
            startup.warmup.assert_not_called()
            callback.assert_called_once()
            self.assertNotIn("Traceback", startup.output.getvalue())


if __name__ == "__main__":
    unittest.main()
