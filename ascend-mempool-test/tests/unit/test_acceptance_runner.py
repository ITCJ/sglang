"""Exercise paired orchestration and child ownership without NPU libraries."""

import concurrent.futures
import http.client
import json
import signal
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from socketserver import BaseRequestHandler, ThreadingTCPServer
from types import SimpleNamespace
from unittest.mock import patch

from test_service_gate import service_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_ticket10 as runner
import verify_model_tokens
from ticket10_processes import Peer, Processes


class TestAcceptanceRunner(unittest.TestCase):
    def test_ready_probe_recovers_from_echoed_request_line(self):
        """A malformed /ready reply is not readiness and must not abort startup."""
        requests = []

        class Handler(BaseRequestHandler):
            def handle(self):
                with self.request.makefile("rb") as stream:
                    line = stream.readline()
                    while stream.readline() not in (b"\r\n", b"\n", b""):
                        pass
                requests.append(line)
                self.request.sendall(
                    line
                    if len(requests) == 1
                    else b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )

        with ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_address[1]}/ready"
            with patch("builtins.print") as warning:
                runner.wait_http(url, 3, lambda: None)
            warning.assert_called_once()
            self.assertIn(url, warning.call_args.args[0])
            self.assertIn("BadStatusLine", warning.call_args.args[0])
        self.assertEqual(requests, [b"GET /ready HTTP/1.1\r\n"] * 2)

    def test_ready_probe_times_out_with_protocol_evidence(self):
        url = "http://127.0.0.1:12345/ready"
        with (
            patch.object(runner.urllib.request, "build_opener") as build_opener,
            patch("builtins.print"),
        ):
            build_opener.return_value.open.side_effect = http.client.BadStatusLine(
                "GET /ready HTTP/1.1\r\n"
            )
            with self.assertRaises(TimeoutError) as failure:
                runner.wait_http(url, 0.01, lambda: None)
        self.assertIn(url, str(failure.exception))
        self.assertIn("BadStatusLine", str(failure.exception))
        self.assertIn("GET /ready", str(failure.exception))

    def test_ready_probe_preserves_watchdog_failure_after_protocol_error(self):
        checks = []

        def watch():
            checks.append(True)
            if len(checks) == 2:
                raise RuntimeError("NPU service exited with status 7")

        with (
            patch.object(runner.urllib.request, "build_opener") as build_opener,
            patch.object(runner.time, "sleep"),
            patch("builtins.print"),
        ):
            build_opener.return_value.open.side_effect = http.client.BadStatusLine(
                "GET /ready HTTP/1.1\r\n"
            )
            with self.assertRaisesRegex(
                RuntimeError, "NPU service exited with status 7"
            ):
                runner.wait_http("http://127.0.0.1:12345/ready", 3, watch)
            self.assertEqual(build_opener.return_value.open.call_count, 1)

    def test_peer_keeps_early_barrier_and_propagates_failure(self):
        left, right = socket.socketpair()
        with Peer(left, 2) as p, Peer(right, 2) as d:
            p.send("ready", phase="transfer")
            self.assertEqual(d.expect("ready"), dict(phase="transfer"))
            d.send("error", message="decode crashed")
            with self.assertRaisesRegex(RuntimeError, "decode crashed"):
                p.expect("done")

    def test_only_owned_process_is_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with Processes(root) as jobs:
                child = jobs.start(
                    "server", [sys.executable, "-c", "import time; time.sleep(60)"]
                )
                self.assertIsNone(child.poll())
            self.assertIsNotNone(child.poll())
            self.assertTrue((root / "server.command.json").exists())

    def test_failed_child_never_reports_completion(self):
        with tempfile.TemporaryDirectory() as folder, Processes(Path(folder)) as jobs:
            child = jobs.start("bad", [sys.executable, "-c", "raise SystemExit(7)"])
            with self.assertRaisesRegex(RuntimeError, "bad.*7"):
                jobs.wait("bad", child, timeout=5)

    def test_failed_process_inspection_still_reaps_owned_child(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs = Processes(Path(folder))
            child = jobs.start(
                "server", [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            with patch(
                "ticket10_processes.subprocess.check_output", side_effect=OSError
            ):
                with self.assertRaises(OSError):
                    jobs.stop()
            self.assertIsNotNone(child.returncode)
            self.assertEqual(jobs.children, [])

    def test_components_only_never_claims_model_regression(self):
        with tempfile.TemporaryDirectory() as folder:
            old_handler = signal.getsignal(signal.SIGTERM)
            self.addCleanup(signal.signal, signal.SIGTERM, old_handler)
            with (
                patch.object(runner, "connect"),
                patch.object(runner, "Peer"),
                patch.object(runner.Acceptance, "preflight"),
                patch.object(runner.Acceptance, "components"),
                patch.object(runner.Acceptance, "model") as model,
            ):
                status = runner.main(
                    [
                        "--role",
                        "prefill",
                        "--prefill-host",
                        "p",
                        "--decode-host",
                        "d",
                        "--run-id",
                        "components-test",
                        "--suite",
                        "components",
                        "--glm51-model-path",
                        "unused",
                        "--log-root",
                        folder,
                    ]
                )
            self.assertEqual(status, 0)
            model.assert_not_called()
            report = json.loads(
                (Path(folder) / "components-test/prefill/summary.json").read_text()
            )
            self.assertEqual(report["glm51_regression"], "not run")

    def test_log_larger_than_one_frame_and_closed_peer(self):
        left, right = socket.socketpair()
        with (
            tempfile.TemporaryDirectory() as folder,
            Peer(left, 2) as p,
            Peer(right, 2) as d,
        ):
            source, dest = Path(folder) / "source.log", Path(folder) / "copy.log"
            source.write_bytes(b"request complete\n" * 12000)
            with concurrent.futures.ThreadPoolExecutor() as threads:
                sending = threads.submit(runner.send_log, d, source)
                runner.receive_log(p, dest)
                sending.result(timeout=3)
            self.assertEqual(dest.read_bytes(), source.read_bytes())

    def test_quick_entry_skips_components_and_reports_smoke_only(self):
        def passed(agent, label, model, *, quick):
            self.assertEqual(label, "glm51")
            self.assertEqual(model, Path("/data_lib/data/models/GLM-5.1-w4a8"))
            self.assertTrue(quick)
            agent.completed("glm51/formal-graph")
            agent.completed("glm51/quick-smoke")

        with tempfile.TemporaryDirectory() as folder:
            old_handler = signal.getsignal(signal.SIGTERM)
            self.addCleanup(signal.signal, signal.SIGTERM, old_handler)
            with (
                patch.object(runner, "connect"),
                patch.object(runner, "Peer"),
                patch.object(runner.Acceptance, "preflight"),
                patch.object(runner.Acceptance, "components") as components,
                patch.object(runner.Acceptance, "model", new=passed),
            ):
                status = runner.main(
                    [
                        "--role",
                        "prefill",
                        "--prefill-host",
                        "p",
                        "--decode-host",
                        "d",
                        "--run-id",
                        "quick",
                        "--suite",
                        "glm51-quick",
                        "--log-root",
                        folder,
                    ]
                )
            self.assertEqual(status, 0)
            components.assert_not_called()
            report = json.loads(
                (Path(folder) / "quick/prefill/summary.json").read_text()
            )
            self.assertEqual(report["glm51_regression"], "quick_smoke_passed")
            self.assertNotIn("glm51/token-comparison", report["stages"])

    def test_model_metadata_rejects_different_producer_with_same_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = dict(
                num_hidden_layers=78,
                index_topk=2048,
                kv_lora_rank=512,
                qk_rope_head_dim=64,
                index_head_dim=128,
                indexer_types=[
                    "full" if i in runner.PRODUCERS else "shared" for i in range(78)
                ],
            )
            (root / "quant_model_description.json").write_text("{}")
            (root / "config.json").write_text(json.dumps(config))
            self.assertEqual(
                len(runner.model_identity(root, "glm52")["indexer_ids"]), 21
            )
            config["indexer_types"][74:76] = ["shared", "full"]
            (root / "config.json").write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "contract"):
                runner.model_identity(root, "glm52")

    def test_two_peers_finish_four_modes_and_check_real_reports(self):
        self.exercise_model_pair(quick=False)

    def test_quick_pair_runs_only_glm51_graph_and_checks_78_indexers(self):
        self.exercise_model_pair(quick=True)

    def exercise_model_pair(self, *, quick):
        """Substitute model executables; keep coordination and result checks real."""
        label = "glm51" if quick else "glm52"
        request_count = 8 if quick else 10
        producers = list(range(78)) if quick else runner.PRODUCERS
        launched = []
        fixture = dict(
            cases=[dict(name="long", input_ids=[1] * 3072, max_new_tokens=128)]
        )
        response = dict(
            text="Adding gives the total. Answer: 42",
            output_ids=[7, 8],
            meta_info=dict(
                prompt_tokens=3072, completion_tokens=2, finish_reason=dict(type="stop")
            ),
        )
        token_report = dict(
            status="passed",
            fixture=fixture,
            results=[dict(case=fixture["cases"][0], response=response)],
        )

        class ModelExecutables:
            def __init__(self, root, env):
                self.root = root
                root.mkdir(parents=True, exist_ok=True)

            def start(self, name, command, **env):
                launched.append((name, command))
                if name == "launcher":
                    role = command[-2]
                    lines = service_logs()[role].splitlines()
                    # Extend the independent service fixture to ten requests.
                    template = [line for line in lines if "room=3 " in line]
                    for room in range(4, request_count + 1):
                        lines += [
                            line.replace("room=3 ", f"room={room} ")
                            .replace("attempt3", f"attempt{room}")
                            .replace("generation=3", f"generation={room}")
                            for line in template
                        ]
                    data = "\n".join(lines).replace(
                        '"model_layers": 2', '"model_layers": 78'
                    )
                    data = data.replace('"layers": 2', '"layers": 78').replace(
                        '"layer_checks": 6', '"layer_checks": 234'
                    )
                    data = data.replace(
                        '"registered_index_k_entries": 2',
                        f'"registered_index_k_entries": {len(producers)}',
                    )
                    data = data.replace(
                        '"indexer_layer_ids": [0, 1]',
                        '"indexer_layer_ids": ' + json.dumps(producers),
                    )
                    if env["GRAPH_MODE"] == "eager":
                        data = "\n".join(
                            line
                            for line in data.splitlines()
                            if "mempool graph_" not in line
                        )
                        data = data.replace(
                            '"replay_forwards": 3', '"replay_forwards": 0'
                        )
                    (self.root / f"{role}.log").write_text(data)
                elif name == "requests":
                    (self.root / "tokens.json").write_text(json.dumps(token_report))
                elif name.startswith("compare-"):
                    verify_model_tokens.compare(
                        json.loads(Path(command[-2]).read_text()),
                        json.loads(Path(command[-1]).read_text()),
                    )
                return SimpleNamespace(poll=lambda: None)

            def wait(self, name, child, *, timeout, check):
                check()

            @staticmethod
            def alive(name, child):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        left, right = socket.socketpair()
        with (
            tempfile.TemporaryDirectory() as folder,
            Peer(left, 5) as p,
            Peer(right, 5) as d,
        ):
            root = Path(folder)
            agents = []
            for role, peer in (("prefill", p), ("decode", d)):
                local = root / role
                local.mkdir()
                args = SimpleNamespace(
                    role=role, prefill_host="p", decode_host="d", timeout=5
                )
                agents.append(runner.Acceptance(args, peer, local))
            with (
                patch.object(runner, "Processes", ModelExecutables),
                patch.object(
                    runner, "wait_http", side_effect=lambda url, timeout, check: check()
                ) as readiness,
                concurrent.futures.ThreadPoolExecutor() as threads,
            ):
                futures = [
                    threads.submit(agent.model, label, root, quick=quick)
                    for agent in agents
                ]
                for future in futures:
                    future.result(timeout=10)
            # The selected Python mini-lb exposes /health; /readiness is Rust-only.
            router_probes = [
                call.args[0]
                for call in readiness.call_args_list
                if "6699" in call.args[0]
            ]
            self.assertEqual(
                router_probes, ["http://127.0.0.1:6699/health"] * (1 if quick else 4)
            )
            for mode in (
                ("formal-graph",) if quick else ("formal-eager", "formal-graph")
            ):
                report = json.loads(
                    (root / "prefill" / label / mode / "service.json").read_text()
                )
                self.assertEqual(report["requests"], request_count)
                self.assertEqual(report["status"], "formal_service_passed")
            self.assertEqual(agents[0].report["stages"], agents[1].report["stages"])
            self.assertEqual(len(agents[0].report["stages"]), 2 if quick else 5)
            if quick:
                self.assertEqual(
                    agents[0].report["stages"],
                    ["glm51/formal-graph", "glm51/quick-smoke"],
                )
                self.assertFalse(
                    any(name.startswith("compare-") for name, _ in launched)
                )
                self.assertIn(
                    "--quick", next(cmd for name, cmd in launched if name == "prepare")
                )


if __name__ == "__main__":
    unittest.main()
