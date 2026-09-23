#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
init_logging "server_bs${BS}"
ascend_environment

export SGLANG_SET_CPU_AFFINITY=1
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export STREAMS_PER_DEVICE=32
export SGLANG_NPU_USE_MLAPO=1
export SGLANG_NPU_USE_MULTI_STREAM=1
export HCCL_BUFFSIZE=1600
export HCCL_OP_EXPANSION_MODE=AIV
export HCCL_SOCKET_IFNAME="${HCCL_SOCKET_IFNAME:-lo}"
export GLOO_SOCKET_IFNAME="${GLOO_SOCKET_IFNAME:-lo}"
# Use the legacy manual start/stop API, as in the supplied reference script.
export SGLANG_PROFILE_V2=0
export SGLANG_NPU_PROFILING=0

RUN_DIR="${SERVER_RUN_DIR:-${RESULTS_DIR}/server_$(date -u +%Y%m%dT%H%M%S)_bs${BS}}"
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
# Worker max_req_len=context_len-1; scheduler caps output at
# max_req_len-input_len-1. Leave additional room across target versions.
CONTEXT_LENGTH="${CONTEXT_LENGTH:-$((INPUT_LEN + OUTPUT_LEN + 256))}"
if (( CONTEXT_LENGTH < INPUT_LEN + OUTPUT_LEN + 2 )); then
    echo 'CONTEXT_LENGTH must cover INPUT_LEN + OUTPUT_LEN + 2 reserved tokens' >&2
    exit 2
fi

# col.sh says graph mode but disables graphs; here graph replay is the default.
# Include BS exactly to avoid attributing graph-padding compute to BS=11.
graph_args=(--cuda-graph-bs 1 "$BS")
if [[ "${GRAPH_MODE:-1}" == 0 ]]; then
    graph_args=(--disable-cuda-graph)
fi
command=("$PYTHON_BIN" -m sglang.launch_server
    --model-path "$MODEL_PATH" --trust-remote-code
    --tp-size 16 --dp-size 1 --nnodes 1 --node-rank 0
    --attention-backend ascend --device npu
    --host "$HOST" --port "$PORT" --watchdog-timeout 9000
    --mem-fraction-static 0.80 --dtype bfloat16 --kv-cache-dtype bfloat16
    --quantization "${QUANTIZATION:-modelslim}"
    --disable-radix-cache --chunked-prefill-size -1
    --max-prefill-tokens "$INPUT_LEN" --context-length "$CONTEXT_LENGTH"
    --max-running-requests "$BS"
    --enable-dp-attention --disable-shared-experts-fusion --enable-dp-lm-head
    --stream-interval 1 --decode-log-interval 1
    "${graph_args[@]}" "$@")
printf '%q ' "${command[@]}" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
printf '%s\n' "$LOG_FILE" > "$RUN_DIR/log_path.txt"
printf 'Checking environment, then starting server. Results: %s\n' "$RUN_DIR" >&3
"$PYTHON_BIN" -u "$INDEX_OVERLAP_DIR/preflight.py" --devices 16 \
    --model-path "$MODEL_PATH" --output-dir "$RUN_DIR"
printf 'Preflight passed; loading model. Follow startup in the log.\n' >&3
"${command[@]}"
