"""Exercise the runner's real process lifecycle without importing torch."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bench import summarize
from run import execute


class RunnerTest(unittest.TestCase):
    def test_worker_error_is_preserved_in_log(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'case.log'
            rc = execute([sys.executable, '-c',
                          "print('dependency missing', flush=True); raise SystemExit(7)"],
                         log, 5, os.environ.copy())
            self.assertEqual(rc, 7)
            self.assertIn('dependency missing', log.read_text())

    def test_timeout_terminates_worker(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'case.log'
            with self.assertRaises(subprocess.TimeoutExpired):
                execute([sys.executable, '-c',
                         'import time; print("started", flush=True); time.sleep(30)'],
                        log, .3, os.environ.copy())
            self.assertIn('started', log.read_text())

    def test_bandwidth_uses_total_wall_time_and_decimal_bytes(self):
        stats = summarize([{'total_s': t, 'submit_s': .01} for t in (1., 2., 3.)],
                          4_000_000_000)
        self.assertEqual(stats['total_s'], {'median_ms': 2000., 'p95_ms': 3000.})
        self.assertEqual(stats['effective_GBps'], 2.)


if __name__ == '__main__':
    unittest.main()
