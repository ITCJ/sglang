"""Exercise paired orchestration and child ownership without NPU libraries."""

import concurrent.futures
import json
import signal
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_service_gate import service_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_ticket10 as runner
import verify_model_tokens
from ticket10_processes import Peer, Processes


class TestAcceptanceRunner(unittest.TestCase):
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
        """Substitute model executables; keep coordination and result checks real."""
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
                if name == "launcher":
                    role = command[-2]
                    lines = service_logs()[role].splitlines()
                    # Extend the independent service fixture to ten requests.
                    template = [line for line in lines if "room=3 " in line]
                    for room in range(4, 11):
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
                        '"registered_index_k_entries": 21',
                    )
                    data = data.replace(
                        '"indexer_layer_ids": [0, 1]',
                        '"indexer_layer_ids": ' + json.dumps(runner.PRODUCERS),
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
                    threads.submit(agent.model, "glm52", root) for agent in agents
                ]
                for future in futures:
                    future.result(timeout=10)
            # The selected Python mini-lb exposes /health; /readiness is Rust-only.
            router_probes = [
                call.args[0]
                for call in readiness.call_args_list
                if "6699" in call.args[0]
            ]
            self.assertEqual(router_probes, ["http://127.0.0.1:6699/health"] * 4)
            for mode in ("formal-eager", "formal-graph"):
                report = json.loads(
                    (root / "prefill/glm52" / mode / "service.json").read_text()
                )
                self.assertEqual(report["requests"], 10)
                self.assertEqual(report["status"], "formal_service_passed")
            self.assertEqual(agents[0].report["stages"], agents[1].report["stages"])
            self.assertEqual(len(agents[0].report["stages"]), 5)


if __name__ == "__main__":
    unittest.main()
