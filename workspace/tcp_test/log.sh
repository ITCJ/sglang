#!/usr/bin/env bash
set -euo pipefail
latest="$(python3 - <<'PY'
from pathlib import Path
paths = list(Path('/tmp').glob('mooncake-tcp-pair-*'))
if not paths:
    raise SystemExit('No TCP test logs found in this container.')
print(max(paths, key=lambda p: p.stat().st_mtime))
PY
)"
echo "Logs: $latest"
for phase in serve write read; do
  if [[ -f "$latest/$phase/probe.log" ]]; then
    echo "$phase: first 35 lines"
    head -n 35 "$latest/$phase/probe.log"
    echo "$phase: last 60 lines"
    tail -n 60 "$latest/$phase/probe.log"
  fi
done
