import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import performance_suite as suite


class SuiteTest(unittest.TestCase):
    def run_with(self, fake):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            args = SimpleNamespace(client_ip="client", store_ip="store", device=0,
                                   warmup=2, repeats=10, timeout=600)
            with contextlib.redirect_stdout(output):
                rc = suite.run_suite(args, root, run_case=fake)
            self.assertEqual((root / "cli.log").read_text(), output.getvalue())
            self.assertTrue((root / "summary.csv").exists())
            return rc, output.getvalue(), json.loads((root / "summary.json").read_text())

    @staticmethod
    def success(command, timeout):
        tokens = int(command[command.index("--tokens") + 1])
        layout = command[command.index("--layout") + 1]
        destination = Path(command[command.index("--output") + 1])
        destination.write_text(json.dumps({
            "status": "ok", "measurement_protocol": "whole_request_v2", "tokens": tokens, "layout": layout,
            "paths": [{"path": suite.PATH_NAMES[c], "correct": True, "median_s": 0.01,
                       "p95_s": 0.02, "effective_gbps": 1} for c in "ACM"],
        }))
        return 0, "native noise should not reach terminal\n"

    def test_full_matrix_and_compact_output(self):
        commands = []

        def fake(command, timeout):
            commands.append(command)
            return self.success(command, timeout)

        rc, terminal, result = self.run_with(fake)
        self.assertEqual(rc, 0)
        self.assertEqual(len(commands), 11)
        self.assertEqual(commands[0][commands[0].index("--repeats") + 1], "1")
        self.assertEqual(len([c for c in result["cases"] if not c["smoke"]]), 10)
        self.assertEqual(result["status"], "ok")
        self.assertIn("ALL_OK", terminal)
        self.assertNotIn("native noise", terminal)
        self.assertIn("L3-L2_Mooncake=10.000 ms", terminal)
        self.assertNotIn("A=", terminal)

    def test_failure_keeps_previous_results_and_stops(self):
        calls = 0

        def fake(command, timeout):
            nonlocal calls
            calls += 1
            return self.success(command, timeout) if calls < 3 else (1, "detail\nF5\n")

        rc, terminal, result = self.run_with(fake)
        self.assertEqual(calls, 3)
        self.assertEqual(rc, 1)
        self.assertEqual(len(result["cases"]), 2)
        self.assertEqual(result["failed_case"], 2)
        self.assertIn("X2 F5", terminal)
        self.assertNotIn("ALL_OK", terminal)

    def test_interrupt_and_timeout_stop_without_next_case(self):
        for error, code, exit_code in ((KeyboardInterrupt(), "STOP", 130),
                                      (subprocess.TimeoutExpired("test", 1), "TIMEOUT", 1)):
            def fake(command, timeout):
                raise error
            rc, terminal, result = self.run_with(fake)
            self.assertEqual(rc, exit_code)
            self.assertEqual(result["cases"], [])
            self.assertIn(f"X0 {code}", terminal)

    def test_zero_exit_without_result_is_not_success(self):
        rc, terminal, result = self.run_with(lambda *_: (0, "ALL_OK\n"))
        self.assertEqual(rc, 1)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("ALL_OK", terminal)


if __name__ == "__main__":
    unittest.main()
