#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 5 ]]; then
    printf 'Usage: bash run_writer_gate.sh RANK P_IP NIC_URL [DEVICE_ID] [REPORT_DIR]\n' >&2
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
"${MEMPOOL_TEST_PYTHON}" -u "${MEMPOOL_TEST_ROOT}/scripts/verify_writer.py" \
    --rank "${MEMPOOL_TEST_RANK}" --head-ip "${MEMPOOL_TEST_HEAD_IP}" \
    --nic-url "${MEMPOOL_TEST_NIC_URL}" --device-id "${MEMPOOL_TEST_DEVICE_ID}" \
    --store-port 18773 --control-port 18774 --pool-id 103 \
    --s-p 8 --s-d 16 --layers 2 --kv-dim 576 --graph-rows 16 \
    --block-dims 24 48 --timeout "${MEMPOOL_TEST_TIMEOUT}" \
    --report "${MEMPOOL_TEST_REPORT_DIR}/writer-rank${MEMPOOL_TEST_RANK}.json" \
    2>&1 | tee "${MEMPOOL_TEST_REPORT_DIR}/writer-rank${MEMPOOL_TEST_RANK}.log"
