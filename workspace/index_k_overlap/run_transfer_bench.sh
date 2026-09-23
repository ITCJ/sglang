#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

# torchrun invokes this branch once per logical NPU. Binding happens before
# importing torch, creating the pinned allocator, or first-touching host pages.
if [[ "${1:-}" == --worker ]]; then
    shift
    bind=()
    if [[ -n "${NUMA_NODES:-}" ]]; then
        IFS=, read -r -a nodes <<< "$NUMA_NODES"
        if (( ${#nodes[@]} != WORLD_SIZE )); then
            echo 'NUMA_NODES must contain one comma-separated node per worker' >&2
            exit 2
        fi
        node="${nodes[$LOCAL_RANK]}"
        if [[ ! "$node" =~ ^[0-9]+$ ]]; then
            echo 'Each NUMA_NODES entry must be a nonnegative node number' >&2
            exit 2
        fi
        bind=(numactl --cpunodebind="$node" --membind="$node")
    fi
    exec "${bind[@]}" "$PYTHON_BIN" "$INDEX_OVERLAP_DIR/transfer_bench.py" "$@"
fi

init_logging "transfer_bs${BS}_n${NPROC:-16}"
ascend_environment
export PYTHON_BIN
export NUMA_NODES="${NUMA_NODES:-}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export GLOO_SOCKET_IFNAME="${GLOO_SOCKET_IFNAME:-lo}"
NPROC="${NPROC:-16}"
TARGET_CTX="${TARGET_CTX:-65536}"
RUN_DIR="${TRANSFER_RUN_DIR:-${RESULTS_DIR}/transfer_$(date -u +%Y%m%dT%H%M%S)_bs${BS}_ctx${TARGET_CTX}_n${NPROC}}"
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd)"
printf '%s\n' "$LOG_FILE" > "$RUN_DIR/log_path.txt"
mkdir -p "$LOGS_DIR/torchrun_$(basename "$RUN_DIR")_$$"
command=("$PYTHON_BIN" -m torch.distributed.run --standalone --nnodes=1
    --log-dir "$LOGS_DIR/torchrun_$(basename "$RUN_DIR")_$$"
    --nproc-per-node="$NPROC" --no-python bash
    "$INDEX_OVERLAP_DIR/run_transfer_bench.sh" --worker
    --batch-size "$BS" --context-len "$TARGET_CTX"
    --index-head-dim 128 --element-bytes 2 --layers 61
    --host-buffers "${HOST_BUFFERS:-2}"
    --warmup "${TRANSFER_WARMUP:-10}" --repeats "${TRANSFER_REPEATS:-50}"
    --chunk-bytes "${COPY_CHUNK_BYTES:-0}"
    --output-dir "$RUN_DIR" "$@")
printf '%q ' "${command[@]}" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
printf 'Running transfer benchmark. Results: %s\n' "$RUN_DIR" >&3
"${command[@]}"
printf 'Transfer benchmark complete. Results: %s\n' "$RUN_DIR" >&3
