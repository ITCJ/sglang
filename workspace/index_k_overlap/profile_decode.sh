#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
init_logging "decode_bs${BS}"
RUN_DIR="${PROFILE_RUN_DIR:-${RESULTS_DIR}/decode_$(date -u +%Y%m%dT%H%M%S)_bs${BS}}"
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
printf '%s\n' "$LOG_FILE" > "$RUN_DIR/log_path.txt"
printf 'Waiting for server and collecting steady decode. Results: %s\n' "$RUN_DIR" >&3
"$PYTHON_BIN" -u "$INDEX_OVERLAP_DIR/profile_decode.py" \
    --base-url "http://${HOST}:${PORT}" --model-path "$MODEL_PATH" \
    --batch-size "$BS" --input-len "$INPUT_LEN" --output-len "$OUTPUT_LEN" \
    --warmup-tokens "${WARMUP_TOKENS:-64}" \
    --profile-seconds "${PROFILE_SECONDS:-1}" --output-dir "$RUN_DIR" "$@"
printf 'Decode capture complete. Results: %s\n' "$RUN_DIR" >&3
