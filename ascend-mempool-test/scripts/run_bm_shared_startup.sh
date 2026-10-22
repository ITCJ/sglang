#!/usr/bin/env bash
# Run in the existing Python/CANN environment, once on each host.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "${MEMPOOL_TEST_PYTHON:-python3}" -u "$script_dir/run_bm_shared_startup.py" "$@"
