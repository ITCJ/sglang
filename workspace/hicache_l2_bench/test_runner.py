"""Exercise the runner's real process lifecycle without importing torch."""
import csv
import os
import json
from unittest.mock import patch
import run as runner
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bench import summarize
from run import execute


class RunnerTest(unittest.TestCase):
    def test_default_suite_saves_every_size_and_both_paths(self):
        self.check_suite(False)

    def test_validation_is_forwarded_to_smoke_and_all_sizes(self):
        self.check_suite(True)

    def test_scatter_only_runs_all_sizes_without_contiguous_cases(self):
        self.check_suite(False, scatter=True)

    def test_preflight_validation_precedes_unvalidated_matrix(self):
        seen = []
        def worker(command, logfile, timeout, env):
            tokens = int(command[command.index('--tokens') + 1])
            layout = 'scattered' if '--scatter' in command else 'contiguous'
            validate = '--validate' in command
            seen.append((tokens, layout, validate, logfile.name))
            output = Path(command[command.index('--output') + 1])
            stats = {'total_s': {'median_ms': 1., 'p95_ms': 2.}, 'effective_GBps': 1.}
            output.write_text(json.dumps({'status': 'ok', 'tokens': tokens, 'layout': layout,
                                          'validation_enabled': validate,
                                          'correct': True if validate else None,
                                          'summary': {'copy_whole': stats, 'hicache_load': stats}}))
            return 0
        with tempfile.TemporaryDirectory() as d, patch.object(runner, 'HERE', Path(d)), \
                patch.object(runner, 'execute', side_effect=worker), \
                patch.object(sys, 'argv', ['run.py', '--preflight-validate']), \
                patch('builtins.print'):
            self.assertEqual(runner.main(), 0)
            self.assertEqual(seen[0][:3], (4096, 'scattered', True))
            self.assertIn('preflight-', seen[0][3])
            self.assertTrue(all(not item[2] for item in seen[1:]))

    def check_suite(self, validate, scatter=False):
        seen = []
        def worker(command, logfile, timeout, env):
            tokens = int(command[command.index('--tokens') + 1])
            layout = 'scattered' if '--scatter' in command else 'contiguous'
            seen.append((tokens, layout))
            self.assertIn(f'{tokens}-{layout}', logfile.name)
            self.assertEqual("--validate" in command, validate)
            output = Path(command[command.index('--output') + 1])
            stats = {'total_s': {'median_ms': 1., 'p95_ms': 2.},
                     'effective_GBps': 1.}
            output.write_text(json.dumps({'status': 'ok', 'tokens': tokens, 'layout': layout, 'validation_enabled': validate,
                                         'correct': True if validate else None, 'summary': {
                'copy_whole': stats, 'hicache_load': stats}}))
            return 0
        with tempfile.TemporaryDirectory() as d, patch.object(runner, 'HERE', Path(d)), \
                patch.object(runner, 'execute', side_effect=worker), \
                patch.object(sys, 'argv', ['run.py'] + (['--validate'] if validate else []) + (['--scatter'] if scatter else [])), patch('builtins.print'):
            self.assertEqual(runner.main(), 0)
            layouts = ['scattered'] if scatter else ['contiguous', 'scattered']
            expected = [(n, layout) for n in [128, 1024, 4096, 16384, 65536, 131072] for layout in layouts]
            self.assertEqual(seen, expected)
            result_dir = next((Path(d) / 'results').iterdir())
            status = json.loads((result_dir / 'status.json').read_text())
            self.assertEqual(status['status'], 'ok')
            self.assertEqual(status['cases'], [dict(tokens=n, layout=l) for n, l in expected])
            with (result_dir / 'summary.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), len(expected) * 2)
            self.assertEqual({(int(r['tokens']), r['layout']) for r in rows}, set(expected))
            self.assertEqual(len(list(result_dir.glob('case-*.json'))), len(expected))

    def test_failed_smoke_does_not_start_larger_cases(self):
        with tempfile.TemporaryDirectory() as d, patch.object(runner, 'HERE', Path(d)), \
                patch.object(runner, 'execute', return_value=7) as worker, \
                patch.object(sys, 'argv', ['run.py']), patch('builtins.print'):
            self.assertEqual(runner.main(), 1)
            self.assertEqual(worker.call_count, 1)
            result_dir = next((Path(d) / 'results').iterdir())
            status = json.loads((result_dir / 'status.json').read_text())
            self.assertEqual(status['failed_tokens'], 128)
            self.assertEqual(status['status'], 'failed')

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
