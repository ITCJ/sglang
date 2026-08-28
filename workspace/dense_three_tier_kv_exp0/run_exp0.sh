#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

# =============================================================================
# Edit this block for the Ascend machine and model.
# =============================================================================
MODEL_PATH="CHANGE_ME"
TOKENIZER_PATH="${MODEL_PATH}"
MODEL_REVISION="CHANGE_ME_MODEL_COMMIT"
PYTHON_BIN="python3"

SERVER_URL="http://127.0.0.1:30000"
SERVER_HOST="0.0.0.0"
SERVER_PORT="30000"
API_KEY=""
ADMIN_API_KEY=""

HICACHE_SIZE_GB="20"
MOONCAKE_WORKER_CONFIG="${SCRIPT_DIR}/mooncake_worker.json"
MOONCAKE_STORE_CONFIG="${SCRIPT_DIR}/mooncake_store.json"

# These exports are inherited by every SGLang server restart in this script.
unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
unset ASCEND_LAUNCH_BLOCKING
export SGLANG_SET_CPU_AFFINITY=1
export SGLANG_NPU_PROFILING=0
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export STREAMS_PER_DEVICE=32
export HCCL_BUFFSIZE=1600
export HCCL_OP_EXPANSION_MODE=AIV

# Add machine-specific Ascend setup here (toolkit paths and NIC names).
# source /usr/local/Ascend/ascend-toolkit/set_env.sh
# source /usr/local/Ascend/nnal/atb/set_env.sh
# export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3
# export YOUR_ASCEND_VARIABLE=value

# Add model/Ascend-specific sglang.launch_server arguments here.
EXTRA_SERVER_ARGS=(
  # --your-extra-argument
)
# =============================================================================

placement="${1:-}"
length="${2:-64k}"
runs="${3:-3}"
if [[ "${placement}" != "local" && "${placement}" != "remote" ]]; then
  echo "Usage: $0 {local|remote} [64k|128k] [runs]" >&2
  exit 2
fi
for value in "${MODEL_PATH}" "${MODEL_REVISION}"; do
  if [[ "${value}" == *CHANGE_ME* ]]; then
    echo "Fill in MODEL_PATH and MODEL_REVISION at the top of $0" >&2
    exit 2
  fi
done
for path in "${MOONCAKE_WORKER_CONFIG}" "${MOONCAKE_STORE_CONFIG}"; do
  if [[ ! -f "${path}" ]]; then
    echo "Missing Mooncake config: ${path}" >&2
    exit 2
  fi
done
if [[ ! "${runs}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Runs must be a positive integer" >&2
  exit 2
fi
case "${length}" in
  64k)
    prefix_len=65408
    max_total_tokens=73728
    ;;
  128k)
    prefix_len=130944
    max_total_tokens=139264
    ;;
  *)
    echo "Length must be 64k or 128k" >&2
    exit 2
    ;;
esac

"${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" validate-mooncake \
  --mooncake-worker-config "${MOONCAKE_WORKER_CONFIG}" \
  --mooncake-store-config "${MOONCAKE_STORE_CONFIG}" \
  --l3-placement "${placement}"
"${PYTHON_BIN}" -c "from mooncake.store import MooncakeDistributedStore"

run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
results_root="${SCRIPT_DIR}/results/${placement}/${length}/${run_stamp}"
manifest="${SCRIPT_DIR}/manifests/${length}.json"
runtime_dir="${SCRIPT_DIR}/runtime"
pid_file="${runtime_dir}/server.pid"
mkdir -p "${results_root}" "$(dirname -- "${manifest}")" "${runtime_dir}"

auth_args=()
if [[ -n "${API_KEY}" ]]; then
  auth_args+=(--api-key "${API_KEY}")
fi
if [[ -n "${ADMIN_API_KEY}" ]]; then
  auth_args+=(--admin-api-key "${ADMIN_API_KEY}")
fi

server_pid=""
server_log=""

read_server_pid() {
  [[ -f "${pid_file}" ]] || return 1
  local pid
  pid="$(<"${pid_file}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${pid}"
}

stop_server() {
  local pid
  pid="$(read_server_pid)" || return 0
  if ! kill -0 "${pid}" 2>/dev/null; then
    rm -f -- "${pid_file}"
    return 0
  fi
  if [[ "$(ps -p "${pid}" -o args= 2>/dev/null || true)" != *"sglang.launch_server"* ]]; then
    echo "Refusing to stop unexpected PID ${pid}" >&2
    return 2
  fi
  kill -TERM -- "-${pid}"
  local deadline=$((SECONDS + 180))
  while kill -0 "${pid}" 2>/dev/null; do
    if ((SECONDS >= deadline)); then
      kill -KILL -- "-${pid}"
      break
    fi
    sleep 2
  done
  rm -f -- "${pid_file}"
  server_pid=""
}

start_server() {
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  server_log="${runtime_dir}/server.${stamp}.$$.log"

  (
    cd "${REPO_ROOT}"
    export PYTHONPATH="${REPO_ROOT}/python${PYTHONPATH:+:${PYTHONPATH}}"
    export SGLANG_HICACHE_MOONCAKE_CONFIG_PATH="${MOONCAKE_WORKER_CONFIG}"
    exec setsid "${PYTHON_BIN}" -m sglang.launch_server \
      --model-path "${MODEL_PATH}" \
      --revision "${MODEL_REVISION}" \
      --host "${SERVER_HOST}" \
      --port "${SERVER_PORT}" \
      --device npu \
      --trust-remote-code \
      --watchdog-timeout 9000 \
      --quantization modelslim \
      --dtype bfloat16 \
      --tp-size 16 \
      --dp-size 16 \
      --enable-dp-attention \
      --enable-dp-lm-head \
      --dcp-size 1 \
      --attention-backend ascend \
      --kv-cache-dtype bfloat16 \
      --page-size 64 \
      --max-running-requests 128 \
      --max-total-tokens "${max_total_tokens}" \
      --enable-hierarchical-cache \
      --hicache-size "${HICACHE_SIZE_GB}" \
      --hicache-write-policy write_through \
      --hicache-io-backend kernel_ascend \
      --hicache-mem-layout page_first_kv_split \
      --hicache-storage-backend mooncake \
      --hicache-storage-prefetch-policy wait_complete \
      --enable-metrics \
      --enable-cache-report \
      "${EXTRA_SERVER_ARGS[@]}"
  ) >>"${server_log}" 2>&1 &
  server_pid=$!
  printf '%s\n' "${server_pid}" >"${pid_file}"
  echo "Started SGLang PID ${server_pid}; log: ${server_log}"

  local curl_auth=()
  if [[ -n "${API_KEY}" ]]; then
    curl_auth=(-H "Authorization: Bearer ${API_KEY}")
  fi
  local deadline=$((SECONDS + 1800))
  until curl -fsS "${curl_auth[@]}" "${SERVER_URL}/health" >/dev/null 2>&1; do
    if ! kill -0 "${server_pid}" 2>/dev/null; then
      rm -f -- "${pid_file}"
      echo "SGLang exited during startup; see ${server_log}" >&2
      return 1
    fi
    if ((SECONDS >= deadline)); then
      echo "Timed out waiting for SGLang health" >&2
      stop_server || true
      return 1
    fi
    sleep 5
  done
}

restart_server() {
  stop_server
  start_server
}

capture_environment() {
  local output="$1"
  capture() {
    local title="$1"
    shift
    printf '\n## %s\n' "${title}"
    if command -v "$1" >/dev/null 2>&1; then
      "$@" || true
    else
      printf '[unavailable: %s]\n' "$1"
    fi
  }
  {
    capture "UTC time" date -u --iso-8601=seconds
    capture "Git commit" git -C "${REPO_ROOT}" rev-parse HEAD
    capture "Kernel" uname -a
    capture "CPU topology" lscpu
    capture "NUMA topology" numactl --hardware
    capture "Network links" ip -details link show
    capture "RDMA links" rdma link show
    capture "InfiniBand mapping" ibdev2netdev
    capture "Ascend devices" npu-smi info
    capture "Ascend topology" npu-smi info -t topo
  } >"${output}" 2>&1
}

cleanup() {
  stop_server || true
}
trap cleanup EXIT

if [[ ! -f "${manifest}" ]]; then
  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" build-manifest \
    --tokenizer "${TOKENIZER_PATH}" \
    --revision "${MODEL_REVISION}" \
    --output "${manifest}" \
    --prefix-len "${prefix_len}" \
    --question-len 128 \
    --num-prefixes 10 \
    --seed 1
fi

capture_environment "${results_root}/environment.txt"
restart_server

set +e
"${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" preflight \
  --url "${SERVER_URL}" \
  --manifest "${manifest}" \
  --server-log "${server_log}" \
  --mooncake-worker-config "${MOONCAKE_WORKER_CONFIG}" \
  --mooncake-store-config "${MOONCAKE_STORE_CONFIG}" \
  --l3-placement "${placement}" \
  --hicache-size-gb "${HICACHE_SIZE_GB}" \
  --output "${results_root}/preflight.json" \
  "${auth_args[@]}"
preflight_rc=$?
set -e
if ((preflight_rc == 3)); then
  echo "Experiment skipped by the capacity guard: ${results_root}" >&2
  exit 0
elif ((preflight_rc != 0)); then
  exit "${preflight_rc}"
fi

for run_number in $(seq 1 "${runs}"); do
  run_id="run-${run_number}"
  run_dir="${results_root}/${run_id}"
  mkdir -p "${run_dir}"

  if ((run_number > 1)); then
    restart_server
  fi

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" reset \
    --url "${SERVER_URL}" "${auth_args[@]}"

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" l2 \
    --url "${SERVER_URL}" \
    --manifest "${manifest}" \
    --server-log "${server_log}" \
    --mooncake-worker-config "${MOONCAKE_WORKER_CONFIG}" \
    --mooncake-store-config "${MOONCAKE_STORE_CONFIG}" \
    --output "${run_dir}/l2.jsonl" \
    --run-id "${run_id}" \
    --length "${length}" \
    --l3-placement "${placement}" \
    --hicache-size-gb "${HICACHE_SIZE_GB}" \
    "${auth_args[@]}"
  cp -- "${server_log}" "${run_dir}/server-l2.log"

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" wait \
    --url "${SERVER_URL}" "${auth_args[@]}"
  restart_server

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" l3 \
    --url "${SERVER_URL}" \
    --manifest "${manifest}" \
    --server-log "${server_log}" \
    --mooncake-worker-config "${MOONCAKE_WORKER_CONFIG}" \
    --mooncake-store-config "${MOONCAKE_STORE_CONFIG}" \
    --output "${run_dir}/l3.jsonl" \
    --run-id "${run_id}" \
    --length "${length}" \
    --l3-placement "${placement}" \
    --hicache-size-gb "${HICACHE_SIZE_GB}" \
    "${auth_args[@]}"
  cp -- "${server_log}" "${run_dir}/server-l3.log"
done

"${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" summarize "${results_root}" \
  --expected-runs "${runs}" \
  --json-output "${results_root}/summary.json" \
  --markdown-output "${results_root}/summary.md"

stop_server
trap - EXIT
echo "Experiment complete: ${results_root}"
