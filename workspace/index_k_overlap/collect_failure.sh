#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
"$PYTHON_BIN" "$INDEX_OVERLAP_DIR/collect_failure.py" \
    --results-dir "$RESULTS_DIR" --logs-dir "$LOGS_DIR" "$@"
