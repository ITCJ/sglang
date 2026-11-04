"""Exercise the public paired runner with real child processes and loopback TCP."""

import concurrent.futures
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_bandwidth as runner


class TestBandwidthRunner(unittest.TestCase):
    def test_pair_collects_reports_and_cleans_owned_workers(self):
        fixture = """import json,sys,time
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text()); p=Path(c['output']); p.mkdir(parents=True)
for name in ('samples','pipeline','background','residency','workloads'):
 (p/(name+'.json')).write_text('[]')
for name in ('mapping','environment'):
 (p/(name+'.json')).write_text('{}')
(p/'status.json').write_text(json.dumps(dict(status='passed',side=c['side'],rank=c['rank'])))
(p/'ready').touch()
while True: time.sleep(1)
"""
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            def invoke(role):
                args = runner.parse_args(
                    [
                        "--role",
                        role,
                        "--prefill-host",
                        "127.0.0.1",
                        "--decode-host",
                        "127.0.0.2",
                        "--run-id",
                        "cpu-fixture",
                        "--devices",
                        "0",
                        "--numa-nodes",
                        "0",
                        "--control-port",
                        str(port),
                        "--timeout",
                        "10",
                        "--log-root",
                        temp,
                        "--suite",
                        "smoke",
                    ]
                )
                return runner.run_pair(
                    args,
                    worker_command=lambda path: [
                        sys.executable,
                        "-c",
                        fixture,
                        str(path),
                    ],
                )

            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                result = list(pool.map(invoke, ("prefill", "decode")))
            self.assertTrue(all(r["cleanup"] == "passed" for r in result))
            self.assertTrue(
                (
                    Path(temp)
                    / "cpu-fixture"
                    / "prefill"
                    / "peer"
                    / "rank-0"
                    / "status.json"
                ).exists()
            )

    def test_worker_failure_returns_nonzero_and_reports_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]

            def invoke(role):
                args = runner.parse_args(
                    [
                        "--role",
                        role,
                        "--prefill-host",
                        "127.0.0.1",
                        "--decode-host",
                        "127.0.0.2",
                        "--run-id",
                        "cpu-failure",
                        "--devices",
                        "0",
                        "--numa-nodes",
                        "0",
                        "--control-port",
                        str(port),
                        "--timeout",
                        "10",
                        "--log-root",
                        temp,
                        "--suite",
                        "smoke",
                    ]
                )
                try:
                    runner.run_pair(
                        args,
                        worker_command=lambda path: [
                            sys.executable,
                            "-c",
                            "raise SystemExit(7)",
                        ],
                    )
                except RuntimeError as error:
                    return "exited" in str(error) and "7" in str(error)
                return False

            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                self.assertEqual(
                    list(pool.map(invoke, ("prefill", "decode"))), [True, True]
                )


if __name__ == "__main__":
    unittest.main()
