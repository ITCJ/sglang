#!/usr/bin/env bash
# Ticket03 S5: the tested GLM-5.1 small-capacity launch, formal or native baseline.
# Usage: bash ascend-mempool-test/scripts/run_service.sh prefill|decode [formal|native] [--dry-run]
# Source the installed Ascend toolkit/ATB environments before running this script.
set -euo pipefail

role=${1:-}
mode=${2:-formal}
dry_run=${3:-}
if [[ ! "$role" =~ ^(prefill|decode)$ || ! "$mode" =~ ^(formal|native)$ ||
      ( -n "$dry_run" && "$dry_run" != --dry-run ) || $# -gt 3 ]]; then
    echo "Usage: $0 prefill|decode [formal|native] [--dry-run]" >&2
    exit 2
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_dir"
P_IP=${P_IP:-10.120.72.31}
D_IP=${D_IP:-10.120.72.32}
MODEL_PATH=${MODEL_PATH:-/data_lib/data/models/GLM-5.1-w4a8}
LOG_DIR=${LOG_DIR:-/tmp/ticket03-s5-$mode}
export PYTHONPATH="$repo_dir/python${PYTHONPATH:+:$PYTHONPATH}"
export SGLANG_SET_CPU_AFFINITY=1
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export STREAMS_PER_DEVICE=32
export USE_VLLM_CUSTOM_ALLREDUCE=1
export ASCEND_MF_STORE_URL="tcp://$P_IP:24670"
export SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=600
export SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD=1
export SGLANG_NPU_ENABLE_MEMPOOL=0
if [[ "$mode" == formal ]]; then export SGLANG_NPU_ENABLE_MEMPOOL=1; fi
export SGLANG_NPU_MEMPOOL_READBACK=0
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=${SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE:-0,2,4,6}
export SGLANG_NPU_MEMPOOL_DIAGNOSTICS=0
export SGLANG_NPU_USE_MLAPO=0
export HCCL_BUFFSIZE=1024
export HCCL_SOCKET_IFNAME=${HCCL_SOCKET_IFNAME:-bond4}
export GLOO_SOCKET_IFNAME=${GLOO_SOCKET_IFNAME:-bond4}
export PYTHONUNBUFFERED=1
unset ASCEND_LAUNCH_BLOCKING ASCEND_MF_TRANSFER_PROTOCOL
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

common=(
    python3 -m sglang.launch_server --model-path "$MODEL_PATH"
    --tp 16 --base-gpu-id 0 --gpu-id-step 1 --trust-remote-code
    --attention-backend ascend --device npu --quantization modelslim
    --watchdog-timeout 9000 --mem-fraction-static 0.75 --context-length 1024
    --disable-radix-cache --chunked-prefill-size -1 --max-prefill-tokens 512
    --enable-dp-attention --dp-size 1 --enable-dp-lm-head --max-running-requests 16
    --prefill-max-requests 1 --disaggregation-transfer-backend ascend
    --disaggregation-mode "$role" --nnodes 1 --node-rank 0
    --moe-dense-tp-size 1 --moe-a2a-backend deepep --disable-shared-experts-fusion
    --load-balance-method round_robin --dtype bfloat16
)
if [[ "$role" == prefill ]]; then
    local_ip=$P_IP
    export DEEPEP_NORMAL_LONG_SEQ_ROUND=72
    export DEEPEP_NORMAL_LONG_SEQ_PER_ROUND_TOKENS=1024
    export DEEPEP_NORMAL_COMBINE_ENABLE_LONG_SEQ=1
    export DEEP_NORMAL_MODE_USE_INT8_QUANT=1
    export TASK_QUEUE_ENABLE=1
    common+=(--host "$local_ip" --port 8000 --disable-cuda-graph
        --disaggregation-bootstrap-port 8995 --deepep-mode normal)
else
    local_ip=$D_IP
    export SGLANG_ENABLE_OVERLAP_PLAN_STREAM=1
    export SGLANG_ENABLE_SPEC_V2=1
    export SGLANG_NPU_USE_MULTI_STREAM=1
    export SGLANG_DEEPEP_NUM_MAX_DISPATCH_TOKENS_PER_RANK=32
    export TASK_QUEUE_ENABLE=0
    common+=(--host "$local_ip" --port 8001 --ep-size 16
        --disaggregation-decode-extra-slots 0 --deepep-mode low_latency
        --cuda-graph-bs-decode 16)
fi
common+=(--dist-init-addr "$local_ip:10000")
if [[ "$mode" == formal ]]; then
    common+=(--mempool-prefill-host "$P_IP" --mempool-bootstrap-port 8995
        --mempool-base-port 19000 --mempool-pool-id 104
        --mempool-nic "tcp://$local_ip:25670" --mempool-prefill-capacity 512
        --mempool-decode-capacity 512 --mempool-timeout 600)
fi
if [[ "$dry_run" == --dry-run ]]; then
    printf 'MEMPOOL=%s READBACK=%s role=%s mode=%s\n' \
        "$SGLANG_NPU_ENABLE_MEMPOOL" "$SGLANG_NPU_MEMPOOL_READBACK" "$role" "$mode"
    printf '%q ' "${common[@]}"
    printf '\n'
    exit 0
fi

mkdir -p "$LOG_DIR"
{
    git rev-parse HEAD
    git status --short
    printf '%q ' "${common[@]}"
    printf '\n'
    # Record launch controls, without collecting unrelated environment secrets.
    for name in SGLANG_SET_CPU_AFFINITY SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT \
        SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD SGLANG_NPU_ENABLE_MEMPOOL \
        SGLANG_NPU_MEMPOOL_READBACK SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE \
        SGLANG_NPU_MEMPOOL_DIAGNOSTICS SGLANG_NPU_USE_MLAPO \
        SGLANG_ENABLE_OVERLAP_PLAN_STREAM SGLANG_ENABLE_SPEC_V2 SGLANG_NPU_USE_MULTI_STREAM \
        SGLANG_DEEPEP_NUM_MAX_DISPATCH_TOKENS_PER_RANK ASCEND_MF_STORE_URL \
        HCCL_BUFFSIZE HCCL_SOCKET_IFNAME GLOO_SOCKET_IFNAME TASK_QUEUE_ENABLE \
        DEEPEP_NORMAL_LONG_SEQ_ROUND DEEPEP_NORMAL_LONG_SEQ_PER_ROUND_TOKENS \
        DEEPEP_NORMAL_COMBINE_ENABLE_LONG_SEQ DEEP_NORMAL_MODE_USE_INT8_QUANT \
        STREAMS_PER_DEVICE PYTORCH_NPU_ALLOC_CONF USE_VLLM_CUSTOM_ALLREDUCE; do
        value=$(printenv "$name") || continue
        printf '%s=%s\n' "$name" "$value"
    done
    python3 -m pip list --format=freeze
    npu-smi info
} > "$LOG_DIR/$role-environment.txt" 2>&1
"${common[@]}" 2>&1 | tee "$LOG_DIR/$role.log"
