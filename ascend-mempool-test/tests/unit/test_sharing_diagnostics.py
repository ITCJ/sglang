"""The diagnostic must retain the boundary that did not finish."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from sharing_worker import check_work


class StreamContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestSharingDiagnostics(unittest.TestCase):
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
