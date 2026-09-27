#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 5 ]]; then
    printf 'Usage: bash run_gate.sh RANK P_IP NIC_URL [DEVICE_ID] [REPORT_DIR]\n' >&2
    exit 2
fi

MEMPOOL_TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMPOOL_TEST_RANK="$1"
MEMPOOL_TEST_HEAD_IP="$2"
MEMPOOL_TEST_NIC_URL="$3"
MEMPOOL_TEST_DEVICE_ID="${4:-0}"
MEMPOOL_TEST_REPORT_DIR="${5:-${MEMPOOL_TEST_ROOT}/reports}"
MEMPOOL_TEST_PYTHON="${MEMPOOL_TEST_PYTHON:-python3}"
MEMPOOL_TEST_TIMEOUT="${MEMPOOL_TEST_TIMEOUT:-600}"

mkdir -p "${MEMPOOL_TEST_REPORT_DIR}"

run_case() {
    local label="$1" decode_tokens="$2" store_port="$3" control_port="$4" pool_id="$5"
    "${MEMPOOL_TEST_PYTHON}" -u "${MEMPOOL_TEST_ROOT}/scripts/verify_graph.py" \
        --rank "${MEMPOOL_TEST_RANK}" --head-ip "${MEMPOOL_TEST_HEAD_IP}" \
        --nic-url "${MEMPOOL_TEST_NIC_URL}" --device-id "${MEMPOOL_TEST_DEVICE_ID}" \
        --layers 2 --slots 16 --s-p 16384 --s-d "${decode_tokens}" \
        --graph-rows 16 --active-rows 3 --topk 64 --block-dims 24 48 \
        --replay-cycles 2 --timeout "${MEMPOOL_TEST_TIMEOUT}" \
        --store-port "${store_port}" --control-port "${control_port}" --pool-id "${pool_id}" \
        --report "${MEMPOOL_TEST_REPORT_DIR}/${label}-rank${MEMPOOL_TEST_RANK}.json" \
        2>&1 | tee "${MEMPOOL_TEST_REPORT_DIR}/${label}-rank${MEMPOOL_TEST_RANK}.log"
}

run_case symmetric 16384 18573 18574 101
run_case asymmetric 32768 18673 18674 102
