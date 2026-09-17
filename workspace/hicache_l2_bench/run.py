#!/usr/bin/env python3
"""Isolated cases with smoke-first execution, persistent logs and bounded runtime."""
import argparse
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from datetime import datetime

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def execute(command, logfile, timeout, env):
    with logfile.open('w') as stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                 env=env, cwd=REPO, start_new_session=True)
        try:
            return child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise


def case_sizes(tokens=None, smoke=False):
    if smoke or tokens == 128:
        return [128]
    if tokens is not None:
        return [128, tokens]
    return [128, 1024, 4096, 16384, 65536, 131072]


def case_matrix(tokens=None, smoke=False, scatter=False):
    layouts = ('scattered',) if scatter else ('contiguous', 'scattered')
    return [(size, layout) for size in case_sizes(tokens, smoke) for layout in layouts]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--suite', action='store_true', help='full suite (also the default)')
    mode.add_argument('--smoke', action='store_true', help='only the 128-token smoke check')
    mode.add_argument('--tokens', type=int, help='smoke then a single token size')
    p.add_argument('--scatter', action='store_true', help='only gapped L2/L1 mapping (default: both layouts)')
    p.add_argument('--device', type=int, default=0)
    p.add_argument('--validate', action='store_true', help='validate every iteration, including smoke (default: off)')
    p.add_argument('--preflight-validate', action='store_true',
                   help='run one validated 4K scattered case before the unvalidated performance matrix')
    p.add_argument('--warmup', type=int, default=2)
    p.add_argument('--repeats', type=int, default=10)
    p.add_argument('--timeout', type=float, default=1800, help='seconds per case')
    p.add_argument('--diagnose', action='store_true', help='print last 35 log lines of latest run')
    args = p.parse_args()
    if args.validate and args.preflight_validate:
        p.error('--validate and --preflight-validate are mutually exclusive')
    if args.preflight_validate and (args.smoke or args.tokens is not None or args.scatter):
        p.error('--preflight-validate is only supported by the full suite')
    if args.diagnose:
        runs = sorted((HERE / 'results').glob('*'))
        if not runs:
            p.error('no results')
        logs = sorted(runs[-1].glob('case-*.log'), key=lambda x: x.stat().st_mtime)
        if not logs:
            p.error('no case logs')
        print(logs[-1])
        print('\n'.join(logs[-1].read_text(errors='replace').splitlines()[-35:]))
        return 0
    if (args.tokens is not None and (args.tokens < 128 or args.tokens > 131072 or args.tokens % 128)) or args.device < 0 or args.warmup < 0 or args.repeats < 1 or args.timeout <= 0:
        p.error('invalid tokens/device/warmup/repeats/timeout')
    directory = HERE / 'results' / datetime.now().strftime('%y%m%d_%H%M%S_%f')
    directory.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env['PYTHONPATH'] = str(REPO / 'python') + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONUNBUFFERED'] = '1'
    cases = [(tokens, layout, False) for tokens, layout in
             case_matrix(args.tokens, args.smoke, args.scatter)]
    if args.preflight_validate:
        cases.insert(0, (4096, 'scattered', True))
    status = dict(status='running', args=vars(args), preflight=None, cases=[])
    def save():
        (directory / 'status.json').write_text(json.dumps(status, indent=2) + '\n')
    def report(line):
        print(line, flush=True)
        with (directory / 'cli.log').open('a') as f:
            f.write(line + '\n')
    save()
    report(f'RESULTS {directory}')
    rows = []
    for tokens, layout, preflight in cases:
        prefix = 'preflight' if preflight else 'case'
        output = directory / f'{prefix}-{tokens}-{layout}.json'
        warmup, repeats = ((0, 1) if preflight or tokens == 128
                           else (args.warmup, args.repeats))
        case_validate = True if preflight else args.validate
        command = [sys.executable, str(HERE / 'bench.py'), '--tokens', str(tokens),
                   '--device', str(args.device), '--warmup', str(warmup),
                   '--repeats', str(repeats), '--output', str(output)]
        if layout == 'scattered':
            command.append('--scatter')
        if case_validate:
            command.append('--validate')
        report(f"RUN kind={'preflight' if preflight else 'performance'} tokens={tokens} layout={layout}")
        try:
            rc = execute(command, directory / f'{prefix}-{tokens}-{layout}.log', args.timeout, env)
            if rc:
                raise RuntimeError(f'worker exit={rc}')
            result = json.loads(output.read_text())
            if (result['status'] != 'ok'
                    or result.get('tokens') != tokens
                    or result.get('layout') != layout
                    or result.get('validation_enabled') is not case_validate
                    or 'correct' not in result
                    or result['correct'] is not (True if case_validate else None)):
                raise RuntimeError('case did not finish successfully')
            if preflight:
                status['preflight'] = dict(status='ok', tokens=tokens, layout=layout)
            else:
                status['cases'].append(dict(tokens=tokens, layout=layout))
            for name, stats in result['summary'].items():
                report(f"tokens={tokens} layout={layout} {name}={stats['total_s']['median_ms']:.3f} ms "
                       f"{stats['effective_GBps']:.3f} GB/s")
                for metric, values in stats.items():
                    if not preflight and isinstance(values, dict):
                        rows.append((tokens, tokens == 128, layout, name, metric,
                                     values['median_ms'], values['p95_ms']))
            with (directory / 'summary.csv').open('w') as f:
                w = csv.writer(f)
                w.writerow(('tokens','smoke','layout','path','metric','median_ms','p95_ms'))
                w.writerows(rows)
            save()
        except (Exception, KeyboardInterrupt) as exc:
            status.update(status='failed', failed_kind='preflight' if preflight else 'performance',
                          failed_tokens=tokens, failed_layout=layout, error=repr(exc))
            save()
            report(f'L2_FAIL tokens={tokens} layout={layout} {type(exc).__name__}; run --diagnose')
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
    status['status'] = 'ok'
    save()
    report('L2_ALL_OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
