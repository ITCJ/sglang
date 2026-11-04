#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "${MEMPOOL_TEST_PYTHON:-python3}" -u "$script_dir/run_bandwidth.py" "$@"
