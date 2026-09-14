import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from feasibility_check import destination, failure_code
from feasibility_log import LogStream


class DestinationTest(unittest.TestCase):
    def test_small_contiguous(self):
        self.assertEqual(destination(0, 1, False), 1)

    def test_max_scattered_covers_all_l1_pages(self):
        slots = [destination(page, 1024, True) for page in range(1024)]
        self.assertEqual(set(slots), set(range(1, 1025)))
        self.assertNotEqual(slots[1], 2)

    def test_short_result_only(self):
        terminal, logfile = io.StringIO(), io.StringIO()
        stream = LogStream(terminal, logfile)
        stream.write("FEASIBILITY_FAIL stage=NPU staging registration\n")
        stream.write("F4\n")
        self.assertEqual(terminal.getvalue(), "F4\n")
        self.assertIn("FEASIBILITY_FAIL", logfile.getvalue())
        self.assertEqual(failure_code("NPU staging registration"), "F4")

    def test_native_and_child_logs_stay_off_terminal(self):
        script = """
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from feasibility_log import enable_log
with patch('feasibility_log.Path', return_value=Path(sys.argv[1])):
    enable_log('client', 'small')
print('Python detail')
os.write(1, b'native stdout\\n')
os.write(2, b'native stderr\\n')
os.write(sys.stdout.fileno(), b'fileno detail\\n')
sys.stdout.buffer.write(b'buffer detail\\n')
sys.stdout.flush()
subprocess.run([sys.executable, '-c', "print('child detail')"], check=True)
print('S0', flush=True)
print('P0', flush=True)
print('D0', flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            logfile = Path(directory) / "check.log"
            result = subprocess.run(
                [sys.executable, "-c", script, str(logfile)],
                cwd=Path(__file__).resolve().parent,
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(result.stdout, "S0\nP0\nD0\n")
            self.assertEqual(result.stderr, "")
            details = logfile.read_text()
            for message in ("Python detail", "native stdout", "native stderr",
                            "fileno detail", "buffer detail", "child detail", "S0", "P0"):
                self.assertIn(message, details)


if __name__ == "__main__":
    unittest.main()
