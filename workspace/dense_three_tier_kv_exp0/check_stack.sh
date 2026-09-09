#!/usr/bin/env bash
# Integration gate before experiment 0. Not a formal latency measurement.
# Start setup/check_fabric_pair.py target on the Store node first.
set -euo pipefail
if [[ $# != 3 ]]; then
  echo 'Usage: bash workspace/dense_three_tier_kv_exp0/check_stack.sh <model-path> <worker-IP> <store-IP>' >&2
  exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$(mktemp -d /tmp/sglang-hicache-check-XXXXXX)"
"${PYTHON_BIN:-python3}" - "$2" "$3" "${STORE_PORT:-50071}" "${CONFIG_DIR}/worker.json" <<'PY'
import ipaddress
import json
from pathlib import Path
import sys
worker, store, port, output = sys.argv[1:]
ipaddress.IPv4Address(worker)
ipaddress.IPv4Address(store)
if not 1024 <= int(port) <= 65535:
    raise SystemExit('Invalid Store port')
Path(output).write_text(json.dumps({
    'local_hostname': worker,
    'metadata_server': 'P2PHANDSHAKE',
    'master_server_address': f'{store}:{port}',
    'protocol': 'ascend', 'device_name': '',
    'global_segment_size': 0, 'local_buffer_size': 0,
}))
PY
echo "Integration config: ${CONFIG_DIR}/worker.json"
echo 'TP16+DP1, 2 GiB HiCache per rank; diagnostic only, not the 64K experiment.'
export MOONCAKE_CONFIG="${CONFIG_DIR}/worker.json"
exec bash "${SCRIPT_DIR}/../setup/run_model.sh" "$1"
