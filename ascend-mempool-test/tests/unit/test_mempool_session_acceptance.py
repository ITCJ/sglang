"""K1 session acceptance keeps distinct checks with two service starts."""

import contextlib
import io
import json
import socket
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import run_mempool_min
import verify_mempool_session
from test_mempool_router import registration
from ticket10_processes import Peer, Processes

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestSessionAcceptance(unittest.TestCase):
    def test_normal_shutdown_keeps_store_alive_until_decode_client_exits(self):
        left, right = socket.socketpair()
        with (
            tempfile.TemporaryDirectory() as folder,
            Peer(left, 5) as p,
            Peer(right, 5) as d,
        ):
            parent = Path(folder)
            marker = parent / "decode-closed"
            config = RouterConfig("p", "d", "/model")
            with contextlib.ExitStack() as stack:
                jobs, children, instances, roots = [], [], [], []
                for role, peer in (("prefill", p), ("decode", d)):
                    root = parent / role
                    ready = root / "ready"
                    owned = stack.enter_context(Processes(root))
                    code = (
                        "import signal,time,sys\nfrom pathlib import Path\n"
                        f"marker=Path({str(marker)!r})\n"
                        "def stop(*args):\n"
                        + (
                            " marker.write_text('closed')\n sys.exit(0)\n"
                            if role == "decode"
                            else " sys.exit(0 if marker.exists() else 2)\n"
                        )
                        + "signal.signal(signal.SIGTERM,stop)\n"
                        + f"Path({str(ready)!r}).touch()\n"
                        + "while True: time.sleep(0.01)\n"
                    )
                    child = owned.start(role, [sys.executable, "-c", code])
                    deadline = time.monotonic() + 5
                    while not ready.exists():
                        if time.monotonic() > deadline:
                            self.fail("local server did not become ready")
                        time.sleep(0.01)
                    instances.append(
                        run_mempool_min.Runner(
                            SimpleNamespace(
                                mode="session-acceptance", role=role, run_id="cpu"
                            ),
                            config,
                            peer,
                            root,
                        )
                    )
                    jobs.append(owned)
                    children.append(child)
                    roots.append(root)
                with ThreadPoolExecutor(2) as workers:
                    futures = [
                        workers.submit(instance.close_session, child, owned, root)
                        for instance, child, owned, root in zip(
                            instances, children, jobs, roots
                        )
                    ]
                    for future in futures:
                        future.result(timeout=10)
                self.assertEqual([c.returncode for c in children], [0, 0])
                self.assertTrue((roots[0] / "decode.log").exists())

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
