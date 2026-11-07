"""Keep historical log parsing and normal startup cleanup without diagnostic hooks."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


import diagnose_target_startup
from run_mempool_min import RouterConfig, Runner


class TestIndexDiagnosis(unittest.TestCase):
    def test_asynchronous_startup_disables_inherited_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                role="decode",
                mode="serve",
                run_id="diag",
                config=root / "config.json",
                without_index_sync=True,
            )
            runner = Runner(
                args, RouterConfig("p", "d", "/model", observe=True), Mock(), root
            )
            jobs = Mock()
            jobs.__enter__ = Mock(return_value=jobs)
            jobs.__exit__ = Mock(return_value=False)
            with (
                patch.dict(os.environ, SGLANG_NPU_MEMPOOL_INDEX_DIAG="1"),
                patch.object(
                    diagnose_target_startup, "Processes", return_value=jobs
                ) as processes,
                patch.object(diagnose_target_startup, "wait_http"),
                patch.object(
                    diagnose_target_startup, "service_ports", return_value=set()
                ),
                patch.object(diagnose_target_startup, "check_warmups") as parser,
            ):
                diagnose_target_startup.startup(runner)
            self.assertEqual(
                processes.call_args.args[1]["SGLANG_NPU_MEMPOOL_INDEX_DIAG"], "0"
            )
            parser.assert_not_called()
            self.assertNotIn("warmups", runner.report)
            self.assertEqual(runner.report["status"], "diagnostic_startup_passed")

    def test_existing_joined_rank_log_is_read_offline_without_starting_services(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "launcher.log"
            lines = [
                "[MEMPOOL_INDEX_DIAG] "
                + json.dumps(
                    dict(
                        pid=2420540 + device,
                        phase="warmup_complete",
                        device=device,
                        warmup=warmup,
                        calls=390,
                    )
                )
                for device in range(16)
                for warmup in range(2)
            ]
            # stdout from several workers may join records before any newline.
            log.write_text("prefix " + "".join(lines) + "[TP0] unrelated log\n")
            with (
                patch.object(
                    diagnose_target_startup.sys,
                    "argv",
                    ["diagnose", "--read-log", str(log)],
                ),
                patch.object(diagnose_target_startup, "connect") as connect,
                patch.object(diagnose_target_startup, "startup") as startup,
            ):
                self.assertEqual(diagnose_target_startup.main(), 0)
            connect.assert_not_called()
            startup.assert_not_called()
            evidence = json.loads((log.parent / "index-diagnosis.json").read_text())
            self.assertEqual(evidence["status"], "warmup_evidence_complete")
            self.assertEqual(len(evidence["completed"]), 32)
            self.assertEqual(evidence["acceptance"], "not-evaluated")

    def test_broken_or_failed_record_cannot_be_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "launcher.log"
            complete = "".join(
                "[MEMPOOL_INDEX_DIAG] "
                + json.dumps(
                    dict(
                        phase="warmup_complete",
                        device=device,
                        warmup=warmup,
                        calls=390,
                    )
                )
                for device in range(16)
                for warmup in range(2)
            )
            for suffix in (
                '[MEMPOOL_INDEX_DIAG] {"phase":broken}',
                '[MEMPOOL_INDEX_DIAG] {"phase":"failed","stage":"before_sync"}',
            ):
                log.write_text(complete + suffix)
                with self.assertRaisesRegex(ValueError, "diagnosis"):
                    diagnose_target_startup.check_warmups(log)

    def test_startup_failure_cleans_up_without_marking_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                role="decode", mode="serve", run_id="diag", config=root / "config.json"
            )
            runner = Runner(
                args, RouterConfig("p", "d", "/model", observe=True), Mock(), root
            )
            jobs = Mock()
            jobs.__enter__ = Mock(return_value=jobs)
            jobs.__exit__ = Mock(return_value=False)
            failure = RuntimeError("NPU service exited unexpectedly with 137")
            jobs.alive.side_effect = failure
            with (
                patch.object(diagnose_target_startup, "Processes", return_value=jobs),
                patch.object(
                    diagnose_target_startup,
                    "wait_http",
                    side_effect=lambda url, timeout, check: check(),
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    diagnose_target_startup.startup(runner)
            self.assertIs(raised.exception, failure)
            jobs.__exit__.assert_called_once()
            self.assertNotEqual(runner.report["status"], "diagnostic_startup_passed")

    def test_offline_warmups_and_normal_startup_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "launcher.log"
            completed = [
                dict(phase="warmup_complete", device=device, warmup=warmup, calls=390)
                for device in range(16)
                for warmup in range(2)
            ]
            log.write_text("[MEMPOOL_INDEX_DIAG] " + json.dumps(completed[0]) + "\n")
            with self.assertRaisesRegex(ValueError, "missing TP16"):
                diagnose_target_startup.check_warmups(log)
            log.write_text(
                "".join(
                    "[MEMPOOL_INDEX_DIAG] " + json.dumps(item) + "\n"
                    for item in completed
                )
            )
            for role in ("prefill", "decode"):
                config = RouterConfig("p", "d", "/model", observe=True)
                args = SimpleNamespace(
                    role=role, mode="serve", run_id="diag", config=root / "config.json"
                )
                runner = Runner(args, config, Mock(), root)
                jobs = Mock()
                jobs.__enter__ = Mock(return_value=jobs)
                jobs.__exit__ = Mock(return_value=False)
                with (
                    patch.object(
                        diagnose_target_startup, "Processes", return_value=jobs
                    ),
                    patch.object(diagnose_target_startup, "wait_http") as http,
                    patch.object(
                        diagnose_target_startup, "service_ports", return_value=set()
                    ),
                ):
                    diagnose_target_startup.startup(runner)
                commands = [call.args[0] for call in jobs.start.call_args_list]
                self.assertEqual(
                    commands,
                    ["router", "launcher"] if role == "prefill" else ["launcher"],
                )
                self.assertTrue(
                    all(call.args[0].endswith("/ready") for call in http.call_args_list)
                )
                jobs.__exit__.assert_called_once()
                self.assertEqual(runner.report["status"], "diagnostic_startup_passed")


if __name__ == "__main__":
    unittest.main()
