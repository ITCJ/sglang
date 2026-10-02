#!/usr/bin/env bash
set -euo pipefail

MEMPOOL_TEST_EVEN_NUMA=0
if [[ "${1:-}" == --even-numa ]]; then
    MEMPOOL_TEST_EVEN_NUMA=1
    shift
fi
if [[ $# -lt 3 || $# -gt 4 || ! "$1" =~ ^[01]$ ]]; then
    printf 'Usage: bash run_bm_startup_gate.sh [--even-numa] RANK P_IP LOCAL_IP [REPORT_DIR]\n' >&2
    exit 2
fi

MEMPOOL_TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMPOOL_TEST_RANK="$1"
MEMPOOL_TEST_HEAD_IP="$2"
MEMPOOL_TEST_LOCAL_IP="$3"
MEMPOOL_TEST_REPORT_ROOT="${4:-${MEMPOOL_TEST_ROOT}/reports/bm-startup}"
MEMPOOL_TEST_PYTHON="${MEMPOOL_TEST_PYTHON:-python3}"
MEMPOOL_TEST_TIMEOUT="${MEMPOOL_TEST_TIMEOUT:-600}"
MEMPOOL_TEST_LAYERS=72
MEMPOOL_TEST_TOKENS=8192
MEMPOOL_TEST_GIB=11
if [[ "$MEMPOOL_TEST_EVEN_NUMA" == 1 ]]; then
    # Fixed reproduction profile for the two hosts with local NUMA IDs 0..7.
    export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
    export SGLANG_NPU_MEMPOOL_DIAGNOSTICS=1
    MEMPOOL_TEST_DEVICES='0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15'
    MEMPOOL_TEST_LAYERS=78
    MEMPOOL_TEST_TOKENS=512
    MEMPOOL_TEST_GIB=1
    printf 'EVEN_NUMA: nodes=[0 2 4 6] pools-per-node=4 GiB-per-node=4 total-local-GiB=16\n'
fi
read -r -a MEMPOOL_TEST_DEVICE_IDS <<< "${MEMPOOL_TEST_DEVICES:-0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15}"
MEMPOOL_TEST_SEEN=" "
for MEMPOOL_TEST_DEVICE_ID in "${MEMPOOL_TEST_DEVICE_IDS[@]}"; do
    if [[ ! "$MEMPOOL_TEST_DEVICE_ID" =~ ^([0-9]|1[0-5])$ || "$MEMPOOL_TEST_SEEN" == *" $MEMPOOL_TEST_DEVICE_ID "* ]]; then
        printf 'MEMPOOL_TEST_DEVICES must contain distinct device IDs from 0 to 15.\n' >&2
        exit 2
    fi
    MEMPOOL_TEST_SEEN+="$MEMPOOL_TEST_DEVICE_ID "
done
if [[ ${#MEMPOOL_TEST_DEVICE_IDS[@]} -eq 0 ]]; then
    printf 'MEMPOOL_TEST_DEVICES must not be empty.\n' >&2
    exit 2
fi

mkdir -p "${MEMPOOL_TEST_REPORT_ROOT}"
MEMPOOL_TEST_RUN_DIR="$(mktemp -d "${MEMPOOL_TEST_REPORT_ROOT}/run.XXXXXX")"
printf 'BM startup rank=%s devices=[%s] per-device=%s GiB reports=%s\n' \
    "$MEMPOOL_TEST_RANK" "${MEMPOOL_TEST_DEVICE_IDS[*]}" "$MEMPOOL_TEST_GIB" "$MEMPOOL_TEST_RUN_DIR"
MEMPOOL_TEST_PIDS=()
trap 'printf "Launcher interrupted; inspect workers listed in %s/pids.tsv and their peers before stopping them.\n" "$MEMPOOL_TEST_RUN_DIR" >&2; exit 130' INT TERM

for MEMPOOL_TEST_DEVICE_ID in "${MEMPOOL_TEST_DEVICE_IDS[@]}"; do
    MEMPOOL_TEST_COMMAND=(
        "$MEMPOOL_TEST_PYTHON" -u "$MEMPOOL_TEST_ROOT/scripts/verify_bm_startup.py"
        --rank "$MEMPOOL_TEST_RANK" --head-ip "$MEMPOOL_TEST_HEAD_IP"
        --device-id "$MEMPOOL_TEST_DEVICE_ID"
        --nic-url "tcp://${MEMPOOL_TEST_LOCAL_IP}:$((25670 + 2 * MEMPOOL_TEST_DEVICE_ID))"
        --store-port "$((18773 + 2 * MEMPOOL_TEST_DEVICE_ID))"
        --control-port "$((18774 + 2 * MEMPOOL_TEST_DEVICE_ID))" --pool-id 103
        --layers "$MEMPOOL_TEST_LAYERS" --s-p "$MEMPOOL_TEST_TOKENS" --s-d "$MEMPOOL_TEST_TOKENS" --kv-dim 576 --graph-rows 16
        --timeout "$MEMPOOL_TEST_TIMEOUT" --log-level 1
        --local-ready-dir "$MEMPOOL_TEST_RUN_DIR/ready"
        --local-devices "${MEMPOOL_TEST_DEVICE_IDS[@]}"
        --report "$MEMPOOL_TEST_RUN_DIR/device-${MEMPOOL_TEST_DEVICE_ID}.json"
    )
    if [[ "${MEMPOOL_TEST_DRY_RUN:-0}" == 1 ]]; then
        printf '%q ' "${MEMPOOL_TEST_COMMAND[@]}"
        printf '\n'
        continue
    fi
    "${MEMPOOL_TEST_COMMAND[@]}" > "$MEMPOOL_TEST_RUN_DIR/device-${MEMPOOL_TEST_DEVICE_ID}.log" 2>&1 &
    MEMPOOL_TEST_PID=$!
    MEMPOOL_TEST_PIDS+=("$MEMPOOL_TEST_PID")
    printf '%s\t%s\n' "$MEMPOOL_TEST_DEVICE_ID" "$MEMPOOL_TEST_PID" >> "$MEMPOOL_TEST_RUN_DIR/pids.tsv"
    printf 'Started device=%s pid=%s\n' "$MEMPOOL_TEST_DEVICE_ID" "$MEMPOOL_TEST_PID"
done
if [[ "${MEMPOOL_TEST_DRY_RUN:-0}" == 1 ]]; then
    printf 'DRY_RUN: no workers were started.\n'
    exit 0
fi

MEMPOOL_TEST_RESULT=0
for MEMPOOL_TEST_INDEX in "${!MEMPOOL_TEST_PIDS[@]}"; do
    if wait "${MEMPOOL_TEST_PIDS[$MEMPOOL_TEST_INDEX]}"; then
        MEMPOOL_TEST_EXIT_CODE=0
        printf 'PASSED device=%s\n' "${MEMPOOL_TEST_DEVICE_IDS[$MEMPOOL_TEST_INDEX]}"
    else
        MEMPOOL_TEST_EXIT_CODE=$?
        MEMPOOL_TEST_RESULT=1
        printf 'FAILED device=%s exit=%s; see its log in %s\n' \
            "${MEMPOOL_TEST_DEVICE_IDS[$MEMPOOL_TEST_INDEX]}" "$MEMPOOL_TEST_EXIT_CODE" "$MEMPOOL_TEST_RUN_DIR" >&2
    fi
    printf '%s\t%s\n' "${MEMPOOL_TEST_DEVICE_IDS[$MEMPOOL_TEST_INDEX]}" "$MEMPOOL_TEST_EXIT_CODE" >> "$MEMPOOL_TEST_RUN_DIR/exits.tsv"
done
if [[ "$MEMPOOL_TEST_EVEN_NUMA" == 1 ]]; then
    if ! "$MEMPOOL_TEST_PYTHON" "$MEMPOOL_TEST_ROOT/scripts/check_bm_even_numa.py" \
        --rank "$MEMPOOL_TEST_RANK" --report-dir "$MEMPOOL_TEST_RUN_DIR"; then
        MEMPOOL_TEST_RESULT=1
    fi
fi
if [[ "$MEMPOOL_TEST_RESULT" == 0 ]]; then
    printf 'ALL_BM_STARTUP_CHECKS_PASSED rank=%s devices=[%s]\n' \
        "$MEMPOOL_TEST_RANK" "${MEMPOOL_TEST_DEVICE_IDS[*]}"
fi
exit "$MEMPOOL_TEST_RESULT"
