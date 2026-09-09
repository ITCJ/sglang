#!/usr/bin/env bash
# Print relevant evidence from the latest local probe; no path copying needed.
set -euo pipefail
"${PYTHON_BIN:-python3}" - "$@" <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import argparse
import re

parser = argparse.ArgumentParser()
parser.add_argument('--pair', action='store_true', help='only the newest two-node run; show all worker phases')
args = parser.parse_args()
if args.pair:
    runs = [path for path in Path('/tmp').glob('mooncake-fabric-pair-*')
            if path.is_dir() and list(path.glob('*/probe.log'))]
    if not runs:
        print('No two-node probe logs found in this container. No local-test fallback.')
        raise SystemExit(1)
    latest_run = max(runs, key=lambda path: path.stat().st_mtime_ns)
    print('Two-node run:', latest_run)
    logs = [latest_run / phase / 'probe.log' for phase in ('serve', 'write', 'read')
            if (latest_run / phase / 'probe.log').is_file()]
else:
    logs = list(Path('/tmp').glob('mooncake-fabric-local-*/probe.log'))
    logs += list(Path('/tmp').glob('mooncake-fabric-pair-*/*/probe.log'))
    if logs:
        logs = [max(logs, key=lambda path: path.stat().st_mtime_ns)]
if not logs:
    print('No Fabric probe log found in this container.')
    raise SystemExit(1)
pattern = re.compile(r'fabric|hixl|adxl|hccs|setup returned|verified|error|fail', re.I)
for log in logs:
    print('\nLog:', log)
    print('Modified UTC:', datetime.fromtimestamp(log.stat().st_mtime, timezone.utc).isoformat())
    stage = log.parent / 'stage'
    if stage.exists():
        print('Last stage:', stage.read_text().strip())
    lines = log.read_text(errors='replace').splitlines()
    matches = [line for line in lines if pattern.search(line)
               and not line.startswith('Loaded runtime libraries:')]
    print('\n'.join(matches) if matches else 'No explicit Fabric allocation/transport evidence found.')
print('Library paths and environment switches alone do not prove Fabric allocation.')
PY
