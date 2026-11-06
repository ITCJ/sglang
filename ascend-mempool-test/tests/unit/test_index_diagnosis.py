"""Exercise real counter dispatch and distinguish earlier failures from the call."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import torch

from ascend_mempool.index_diagnosis import warmup_index_diagnosis
from ascend_mempool.observation_device import PageObservation
import diagnose_target_startup
from run_mempool_min import RouterConfig, Runner


def records(output):
    return [
        json.loads(line.split("] ", 1)[1])
        for line in output.getvalue().splitlines()
        if line.startswith("[MEMPOOL_INDEX_DIAG] ")
    ]


class TestIndexDiagnosis(unittest.TestCase):
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
                    diagnose_target_startup.startup(runner, root / "library.so")
            self.assertIs(raised.exception, failure)
            jobs.__exit__.assert_called_once()
            self.assertNotEqual(runner.report["status"], "diagnostic_startup_passed")

    def test_real_counter_is_unchanged_and_mode_leaves_before_capture(self):
        observation = PageObservation(1, 3, 8, 4, 2, (0,), 0, "cpu")
        rows = torch.tensor([2, 0])
        valid = torch.tensor([[True, True], [False, False]])
        hits = torch.tensor([[True, False], [False, False]])
        npu = Mock(current_device=Mock(return_value=7))
        npu.current_stream.return_value.npu_stream = 40
        output = io.StringIO()
        with (
            patch.dict(os.environ, SGLANG_NPU_MEMPOOL_INDEX_DIAG="1"),
            patch.object(torch, "npu", npu, create=True),
            contextlib.redirect_stdout(output),
            torch.inference_mode(),
        ):
            with warmup_index_diagnosis(0):
                observation.cache_updates(0, rows, torch.tensor([2, 0]), hits, valid)
            calls = npu.synchronize.call_count
            # Capture's subsequent call must not be intercepted or synchronized.
            observation.cache_updates(0, rows, torch.tensor([2, 0]), hits, valid)
            self.assertEqual(npu.synchronize.call_count, calls)
        torch.testing.assert_close(
            observation.cache_changes[0],
            torch.tensor([[0, 0, 0], [0, 0, 0], [2, 4, 4]]),
        )
        log = records(output)
        self.assertEqual(log[-1]["phase"], "warmup_complete")
        self.assertEqual(log[-1]["calls"], 1)
        inputs = next(row for row in log if row["phase"] == "inputs")
        self.assertEqual(inputs["indices"], [2, 0])
        self.assertTrue(any("cache_updates" in site for site in inputs["sites"]))
        with (
            patch.dict(os.environ, SGLANG_NPU_MEMPOOL_INDEX_DIAG="0"),
            patch.object(torch, "npu", npu, create=True),
            warmup_index_diagnosis(0),
        ):
            observation.cache_changes[0].index_add_(
                0, rows, torch.ones(2, 3, dtype=torch.long)
            )
            self.assertEqual(npu.synchronize.call_count, calls)

    def test_failed_sync_identifies_boundary_and_restores_dispatch(self):
        for side_effect, stage, expected in (
            ([RuntimeError("prior task")], "before_sync", 0),
            ([None, RuntimeError("index task")], "after_sync", 1),
        ):
            with self.subTest(stage=stage):
                target = torch.zeros(2, 1, dtype=torch.int64)
                rows, update = torch.tensor([1]), torch.ones(1, 1, dtype=torch.int64)
                npu = Mock(current_device=Mock(return_value=7))
                npu.current_stream.return_value.npu_stream = 40
                npu.synchronize.side_effect = side_effect
                output = io.StringIO()
                with (
                    patch.dict(os.environ, SGLANG_NPU_MEMPOOL_INDEX_DIAG="1"),
                    patch.object(torch, "npu", npu, create=True),
                    contextlib.redirect_stdout(output),
                ):
                    with self.assertRaises(RuntimeError):
                        with warmup_index_diagnosis(0):
                            target.index_add_(0, rows, update)
                    self.assertEqual(target[1, 0].item(), expected)
                    target.index_add_(0, rows, update)
                failed = next(
                    row for row in records(output) if row["phase"] == "failed"
                )
                self.assertEqual(failed["stage"], stage)
                self.assertEqual(target[1, 0].item(), expected + 1)

    def test_ready_requires_all_rank_warmups_and_exits_after_one_startup(self):
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
                    diagnose_target_startup.startup(runner, root / "library.so")
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
