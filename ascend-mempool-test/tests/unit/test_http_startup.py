"""Exercise native PD warmup and GC freeze through a delayed local HTTP listener."""

import asyncio
import json
import logging
import os
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import requests
from test_native_release import load_functions


class TestNativeHTTPWarmup(unittest.TestCase):
    def test_each_side_waits_for_http_then_sends_local_fake_warmup_before_freeze(self):
        for role in ("prefill", "decode"):
            with self.subTest(role=role):
                self.run_warmup(role)

    def run_warmup(self, role):
        calls, threads = [], []
        tokenizer = SimpleNamespace(server_status="starting")

        class Handler(BaseHTTPRequestHandler):
            def reply(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                calls.append((self.path, None))
                self.reply(dict(is_generation=True))

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                calls.append((self.path, json.loads(body) if body else None))
                self.reply({})

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        server.server_bind()  # The port exists, but connections are still refused.
        url = f"http://127.0.0.1:{server.server_address[1]}"
        original_get = requests.get

        def probe(*args, **kwargs):
            try:
                return original_get(*args, **kwargs)
            except requests.ConnectionError:
                calls.append(("listener-not-ready", None))
                server.server_activate()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                threads.append(thread)
                thread.start()
                raise

        namespace = self.startup_functions(role, tokenizer)
        try:
            with (
                patch.dict(
                    os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
                ),
                patch.object(requests, "get", side_effect=probe),
                self.assertLogs("native-warmup-test", level="INFO") as logs,
            ):
                namespace["_wait_and_warmup"](
                    SimpleNamespace(url=lambda: url),
                    launch_callback=lambda: calls.append(("ready", None)),
                )
            self.assertEqual(
                [path for path, _ in calls],
                [
                    "listener-not-ready",
                    "/model_info",
                    "/generate",
                    "/freeze_gc",
                    "ready",
                ],
            )
            self.assertEqual(
                calls[2][1],
                dict(
                    sampling_params=dict(
                        temperature=0.0, max_new_tokens=8, ignore_eos=True
                    ),
                    bootstrap_host="2.2.2.2",
                    bootstrap_room=0,
                    input_ids=[10, 11, 12, 13],
                    routed_dp_rank=0,
                ),
            )
            self.assertEqual(tokenizer.server_status, "up")
            self.assertTrue(
                any("End of disaggregation warmup" in line for line in logs.output)
            )
            self.assertFalse(
                any("failed" in line or "Traceback" in line for line in logs.output)
            )
        finally:
            if threads:
                server.shutdown()
                threads[0].join(timeout=5)
            server.server_close()

    def startup_functions(self, role, tokenizer):
        namespace = dict(
            requests=requests,
            aiohttp=aiohttp,
            asyncio=asyncio,
            ssl=ssl,
            os=os,
            logger=logging.getLogger("native-warmup-test"),
            time=SimpleNamespace(sleep=lambda seconds: None),
            envs=SimpleNamespace(
                SGLANG_RUST_SERVER=SimpleNamespace(get=lambda: False),
                SGLANG_WARMUP_TIMEOUT=SimpleNamespace(get=lambda: 10),
            ),
            get_serving=lambda: SimpleNamespace(
                api_key=None,
                admin_api_key=None,
                skip_server_warmup=False,
                skip_tokenizer_init=False,
            ),
            get_model=lambda: SimpleNamespace(
                checkpoint_engine_wait_weights_before_ready=False,
                delete_ckpt_after_loading=False,
            ),
            get_exec=lambda: SimpleNamespace(
                moe=SimpleNamespace(is_ep_scale_joiner=False)
            ),
            get_parallel=lambda: SimpleNamespace(dp_size=1),
            get_disagg=lambda: SimpleNamespace(disaggregation_mode=role),
            get_observability=lambda: SimpleNamespace(
                debug_tensor_dump_input_file=None
            ),
            ssl_verify_of=lambda args: True,
            get_exception_traceback=lambda: "listener not ready",
            kill_process_tree=lambda pid: self.fail("native warmup failed"),
            FAKE_BOOTSTRAP_HOST="2.2.2.2",
            _global_state=SimpleNamespace(tokenizer_manager=tokenizer),
            ServerStatus=SimpleNamespace(Up="up", UnHealthy="unhealthy"),
        )
        load_functions(
            "entrypoints/http_server.py",
            {
                "_send_disaggregation_warmup_requests",
                "_execute_server_warmup",
                "_freeze_gc_after_server_warmup",
                "_wait_and_warmup",
            },
            namespace,
        )
        return namespace
