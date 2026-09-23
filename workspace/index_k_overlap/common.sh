#!/usr/bin/env bash
# Source from the target Ascend host/container; does not start any workload.
INDEX_OVERLAP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODEL_PATH="${MODEL_PATH:-/home/caofei/DeepSeek-V3.2-Exp-w8a8}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-6699}"
BS="${BS:-11}"
INPUT_LEN="${INPUT_LEN:-2048}"
OUTPUT_LEN="${OUTPUT_LEN:-512}"
RESULTS_DIR="${RESULTS_DIR:-${INDEX_OVERLAP_DIR}/results}"
LOGS_DIR="${LOGS_DIR:-${INDEX_OVERLAP_DIR}/logs}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

init_logging() {
    local label="$1"
    mkdir -p "$LOGS_DIR"
    LOGS_DIR="$(cd "$LOGS_DIR" && pwd)"
    LOG_FILE="$LOGS_DIR/${label}_$(date -u +%Y%m%dT%H%M%S)_$$.log"
    exec 3>&1
    printf 'Log: %s\n' "$LOG_FILE" >&3
    exec >"$LOG_FILE" 2>&1
    trap 'run_status=$?; if (( run_status != 0 )); then printf "Failed (exit %s). See log: %s\n" "$run_status" "$LOG_FILE" >&3; fi' EXIT
}

ascend_environment() {
    # Vendor environment scripts may read unset variables.
    set +u
    source "${ASCEND_TOOLKIT_ENV:-/usr/local/Ascend/ascend-toolkit/set_env.sh}"
    source "${ASCEND_ATB_ENV:-/usr/local/Ascend/nnal/atb/set_env.sh}"
    set -u
    export PATH="${BISHENG_BIN:-/usr/local/Ascend/8.5.0/compiler/bishengir/bin}:$PATH"
    unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY ASCEND_LAUNCH_BLOCKING
}
