#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
RUN_DIR="${PROFILE_RUN_DIR:-${RESULTS_DIR}/decode_$(date -u +%Y%m%dT%H%M%S)_bs${BS}}"
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
"$PYTHON_BIN" -u "$INDEX_OVERLAP_DIR/profile_decode.py" \
    --base-url "http://${HOST}:${PORT}" --model-path "$MODEL_PATH" \
    --batch-size "$BS" --input-len "$INPUT_LEN" --output-len "$OUTPUT_LEN" \
    --warmup-tokens "${WARMUP_TOKENS:-64}" \
    --profile-seconds "${PROFILE_SECONDS:-1}" --output-dir "$RUN_DIR" "$@" \
    2>&1 | tee "$RUN_DIR/client.log"
