#!/usr/bin/env bash
# Print relevant evidence from the latest local probe; no path copying needed.
set -euo pipefail
"${PYTHON_BIN:-python3}" - <<'PY'
from pathlib import Path
import re

logs = list(Path('/tmp').glob('mooncake-fabric-local-*/probe.log'))
logs += list(Path('/tmp').glob('mooncake-fabric-pair-*/*/probe.log'))
if not logs:
    print('No local Fabric probe log found in this container.')
    raise SystemExit(1)
latest = max(logs, key=lambda path: path.stat().st_mtime_ns)
print('Log:', latest)
lines = latest.read_text(errors='replace').splitlines()
pattern = re.compile(r'fabric|hixl|adxl|hccs|setup returned|verified|error|fail', re.I)
matches = [line for line in lines if pattern.search(line)
           and not line.startswith('Loaded runtime libraries:')]
if matches:
    print('\n'.join(matches))
else:
    print('No explicit Fabric allocation/transport evidence found.')
print('Library paths and environment switches alone do not prove Fabric allocation.')
PY
