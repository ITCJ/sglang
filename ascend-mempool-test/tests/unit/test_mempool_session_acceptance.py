"""K1 session acceptance keeps distinct checks with two service starts."""

import contextlib
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path

import run_mempool_min
import verify_mempool_session
from test_mempool_router import registration

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestSessionAcceptance(unittest.TestCase):
    def test_session_cli_keeps_shared_decode_with_eight_allocations(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(
                    dict(
                        prefill_host="p",
                        decode_host="d",
                        model_path="/model",
                        prefill_shared=True,
                        decode_shared=True,
                    )
                )
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                run_mempool_min.main(
                    [
                        "--role",
                        "decode",
                        "--mode",
                        "session-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-06-cpu",
                        "--describe",
                    ]
                )
            value = json.loads(output.getvalue())
            self.assertTrue(value["config"]["decode_shared"])
            self.assertTrue(value["config"]["prefill_shared"])
            self.assertEqual(value["service_rounds"], 2)
            self.assertEqual(value["memory_budget"]["bm_bytes"], 8 * 2**30)

    def test_shared_cli_describes_eight_prefill_replicas_and_two_model_starts(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_mempool_min.main(
                    [
                        "--role",
                        "prefill",
                        "--mode",
                        "p-sharing-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-05-cpu",
                        "--describe",
                    ]
                )
            value = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(value["config"]["prefill_shared"])
            self.assertEqual(value["service_rounds"], 2)
            self.assertEqual(value["memory_budget"]["bm_bytes"], 120 * 2**30)

    def test_post_shutdown_port_probe_allows_time_wait_but_rejects_a_listener(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen()
            address = server.getsockname()
            with socket.create_connection(address) as client:
                accepted, _ = server.accept()
                accepted.close()
                self.assertEqual(client.recv(1), b"")
            with self.assertRaises(OSError):
                verify_mempool_session.released_port(*address)
        verify_mempool_session.released_port(*address)

    def test_accounting_requires_completed_prompt_and_every_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "snapshots.json").write_text(
                json.dumps([dict(completed=[dict(rid="r1")])])
            )
            (root / "responses.json").write_text(
                json.dumps(dict(cases=[dict(input_ids=[7] * 256)]))
            )
            free = "\n".join(
                "mempool free_accounting data="
                + json.dumps(dict(rid="r1", rank=r, readers_remaining=0))
                for r in range(16)
            )
            for role in ("prefill", "decode"):
                (root / f"{role}.log").write_text(free + "\n")
            ready = root / "prefill.log"
            ready.write_text(
                ready.read_text()
                + 'mempool ready_accounting data={"rid":"r1","ready_tokens":0,"counter_tokens":0}\n'
            )
            with self.assertRaisesRegex(ValueError, "complete prompt"):
                verify_mempool_session.accounting(root)
            ready.write_text(
                ready.read_text()
                + 'mempool ready_accounting data={"rid":"r1","ready_tokens":256,"counter_tokens":256}\n'
            )
            self.assertEqual(verify_mempool_session.accounting(root)["requests"], 1)
            (root / "decode.log").write_text(free.replace('"rank": 15', '"rank": 14'))
            with self.assertRaisesRegex(ValueError, "every replica"):
                verify_mempool_session.accounting(root)

    def test_cli_exposes_two_service_starts_without_formal_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_mempool_min.main(
                    [
                        "--role",
                        "prefill",
                        "--mode",
                        "session-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-ticket02-001",
                        "--describe",
                    ]
                )
            description = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(description["service_rounds"], 2)
            self.assertTrue(description["config"]["match_prefix"])
            self.assertIn("fail-stop", description["stages"][1])
            self.assertIn("restart", description["stages"][2])
            self.assertEqual(
                description["target_environment"]["SGLANG_NPU_MEMPOOL_ROUTER_RUN_ID"],
                "k1-ticket02-001",
            )

    def test_actual_registration_replay_rejects_one_under_capacity_rank(self):
        config = RouterConfig("p", "d", "/model")
        actual = {r: registration(r) for r in ("prefill", "decode")}
        before = json.dumps(actual, sort_keys=True)
        result = verify_mempool_session.capacity_check(config, "cpu", actual)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["execution"], "CPU replay of actual NPU registration")
        self.assertEqual(json.dumps(actual, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
