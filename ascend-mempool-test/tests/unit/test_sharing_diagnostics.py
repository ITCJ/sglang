"""The diagnostic must retain the boundary that did not finish."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from sharing_worker import check_work, delayed_publication


class StreamContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def export_chrome_trace(self, path):
        pass


class TestSharingDiagnostics(unittest.TestCase):
    def publication(self, selected_ms, writer_ms):
        work, pipeline, cohort = Mock(), Mock(), Mock()
        # Both events completed before the host got scheduled to inspect them.
        pipeline.writer_done.query.return_value = True
        pipeline.ready.recorded_time.return_value = 1_000_000
        pipeline.selected_ready.recorded_time.return_value = (
            1_000_000 + int(selected_ms * 1000)
            if selected_ms == selected_ms
            else selected_ms
        )
        pipeline.writer_done.recorded_time.return_value = 1_000_000 + int(
            writer_ms * 1000
        )
        npu = Mock()
        npu.stream.side_effect = lambda *a: StreamContext()
        npu.graph.side_effect = lambda *a, **k: StreamContext()
        profiler = Mock()
        profiler.profiler.profile.return_value = StreamContext()
        # Profiling uses the context value to export a trace.
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("sharing_worker.CopyWorkload", return_value=work),
            patch("sharing_worker.Pipeline", return_value=pipeline),
            patch("sharing_worker.importlib.import_module", return_value=profiler),
        ):
            return delayed_publication(
                SimpleNamespace(member=1),
                cohort,
                dict(topk=2048, block_dims=[24], delay_matmuls=512),
                SimpleNamespace(npu=npu),
                Path(directory),
            )

    def test_late_host_observation_does_not_reject_earlier_device_selected(self):
        rows = self.publication(2.0, 5.0)
        eager = [r for r in rows if r["engine"] == "eager"]
        self.assertTrue(all(r["selected_before_writer_done"] for r in eager))
        self.assertTrue(all(r["writer_minus_selected_ms"] == 3.0 for r in eager))

    def test_device_order_without_overlap_still_fails(self):
        for selected, writer in ((5.0, 2.0), (2.0, 2.0), (float("nan"), 5.0)):
            with self.subTest(selected=selected, writer=writer):
                with self.assertRaises(AssertionError):
                    self.publication(selected, writer)

    def test_device_sync_failure_keeps_boundary_and_never_announces_drained(self):
        work, pipeline, cohort = Mock(), Mock(), Mock()
        work.has_write = True
        npu = Mock()
        npu.stream.return_value = StreamContext()
        npu.synchronize.side_effect = [None, RuntimeError("device stalled")]
        progress = []
        with self.assertRaisesRegex(RuntimeError, "device stalled"):
            check_work(
                work,
                pipeline,
                cohort,
                SimpleNamespace(npu=npu),
                Mock(),
                "48/write_then_read-16/consumer-0/eager/check",
                0,
                progress=progress.append,
            )
        self.assertTrue(progress[-1].endswith("/device-sync"))
        self.assertFalse(any("copies-drained" in p for p in progress))
        pipeline.verify.assert_not_called()

    def test_watchdog_emits_stack_and_preserves_last_progress_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys, time, faulthandler\n"
                    "from pathlib import Path\n"
                    "sys.path.insert(0, sys.argv[1])\n"
                    "import sharing_worker as worker\n"
                    "original = faulthandler.dump_traceback_later\n"
                    "worker.faulthandler.dump_traceback_later = "
                    "lambda *a, **k: original(0.05)\n"
                    "worker.checkpoint(Path(sys.argv[2]), 'case/device-sync')\n"
                    "time.sleep(0.2)\n"
                    "faulthandler.cancel_dump_traceback_later()\n",
                    str(SCRIPTS),
                    directory,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Timeout", result.stderr)
            self.assertIn('File "<string>"', result.stderr)
            progress = json.loads((Path(directory) / "progress.json").read_text())
            self.assertEqual(progress["phase"], "case/device-sync")
            self.assertIn("timestamp", progress)


if __name__ == "__main__":
    unittest.main()
