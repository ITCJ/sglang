#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
stage="${1:-0}"
if [[ ! "$stage" =~ ^[0-6]$ ]]; then
    echo 'Usage: bash launch_incremental.sh [0-6]' >&2
    exit 2
fi
init_logging "incremental_stage${stage}"
run_dir="$RESULTS_DIR/incremental_stage${stage}_$(date -u +%Y%m%dT%H%M%S)_$$"
mkdir -p "$run_dir"
run_dir="$(cd "$run_dir" && pwd)"
printf '%s\n' "$LOG_FILE" > "$run_dir/log_path.txt"
# No preflight, environment setup, quantization override or graph change here.
# Stage 0 runs the original script content, including its system tuning and
# environment setup, under plain bash just like `bash col.sh`.
baseline="${BASELINE_SCRIPT:-$INDEX_OVERLAP_DIR/../col.sh}"
"$PYTHON_BIN" "$INDEX_OVERLAP_DIR/launch_incremental.py" \
    --source "$baseline" --stage "$stage" --output "$run_dir/server.sh"
# Logging destination only; also keep optional later-stage capture out of /tmp.
export SGLANG_TORCH_PROFILER_DIR="$run_dir/startup_profile"
printf 'Stage %s. Saved script: %s/server.sh\n' "$stage" "$run_dir" >&3
bash "$run_dir/server.sh"
