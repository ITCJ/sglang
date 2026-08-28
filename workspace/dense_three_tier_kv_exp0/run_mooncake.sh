#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# =============================================================================
# Edit this block for the machine running Mooncake.
# =============================================================================
PYTHON_BIN="python3"
MOONCAKE_MASTER_BIN="mooncake_master"
STORE_CONFIG="${SCRIPT_DIR}/mooncake_store.json"

MASTER_PORT="50051"
METADATA_PORT="8080"
STORE_PORT="8081"

# Add machine-specific Mooncake/RDMA environment variables here.
# export MC_MS_AUTO_DISC=1
# export MC_MS_FILTERS=mlx5_0,mlx5_1
# =============================================================================

if [[ ! -f "${STORE_CONFIG}" ]]; then
  echo "Missing Mooncake store config: ${STORE_CONFIG}" >&2
  exit 2
fi
if grep -q "CHANGE_ME" "${STORE_CONFIG}"; then
  echo "Replace every CHANGE_ME value in ${STORE_CONFIG}" >&2
  exit 2
fi
if ! command -v "${MOONCAKE_MASTER_BIN}" >/dev/null 2>&1; then
  echo "Missing Mooncake master executable: ${MOONCAKE_MASTER_BIN}" >&2
  exit 2
fi
if ! "${PYTHON_BIN}" -c \
  "from mooncake.store import MooncakeDistributedStore; import mooncake.mooncake_store_service"; then
  echo "${PYTHON_BIN} cannot import the Mooncake store package" >&2
  exit 2
fi
if [[ "$(ulimit -l)" != "unlimited" ]]; then
  echo "Warning: memlock is not unlimited; 640 GB RDMA registration may fail" >&2
fi

runtime_dir="${SCRIPT_DIR}/runtime/mooncake"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
master_log="${runtime_dir}/master.${stamp}.log"
store_log="${runtime_dir}/store.${stamp}.log"
mkdir -p "${runtime_dir}"

port_open() {
  local port="$1"
  (exec 3<>"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
}

for port in "${MASTER_PORT}" "${METADATA_PORT}" "${STORE_PORT}"; do
  if port_open "${port}"; then
    echo "Local port ${port} is already in use" >&2
    exit 2
  fi
done

master_pid=""
store_pid=""

stop_group() {
  local pid="$1"
  [[ -n "${pid}" ]] || return 0
  kill -0 "${pid}" 2>/dev/null || return 0
  kill -TERM -- "-${pid}" 2>/dev/null || true
  local deadline=$((SECONDS + 30))
  while kill -0 "${pid}" 2>/dev/null; do
    if ((SECONDS >= deadline)); then
      kill -KILL -- "-${pid}" 2>/dev/null || true
      break
    fi
    sleep 1
  done
}

cleanup() {
  set +e
  stop_group "${store_pid}"
  stop_group "${master_pid}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_port() {
  local port="$1"
  local pid="$2"
  local name="$3"
  local log="$4"
  local deadline=$((SECONDS + 1800))
  until port_open "${port}"; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "${name} exited during startup; see ${log}" >&2
      return 1
    fi
    if ((SECONDS >= deadline)); then
      echo "Timed out waiting for ${name} on port ${port}; see ${log}" >&2
      return 1
    fi
    sleep 2
  done
}

setsid "${MOONCAKE_MASTER_BIN}" \
  --port "${MASTER_PORT}" \
  --enable_http_metadata_server=true \
  --http_metadata_server_port="${METADATA_PORT}" \
  --eviction_high_watermark_ratio=0.95 \
  >"${master_log}" 2>&1 &
master_pid=$!
wait_for_port "${MASTER_PORT}" "${master_pid}" "Mooncake master" "${master_log}"
wait_for_port "${METADATA_PORT}" "${master_pid}" "Mooncake metadata service" "${master_log}"

setsid "${PYTHON_BIN}" -m mooncake.mooncake_store_service \
  --config="${STORE_CONFIG}" \
  --port="${STORE_PORT}" \
  >"${store_log}" 2>&1 &
store_pid=$!
wait_for_port "${STORE_PORT}" "${store_pid}" "Mooncake store" "${store_log}"

echo "Mooncake ready; master log: ${master_log}; store log: ${store_log}"
echo "Keep this process running until run_exp0.sh finishes."

set +e
wait -n "${master_pid}" "${store_pid}"
status=$?
set -e
echo "A Mooncake process exited unexpectedly; see ${master_log} and ${store_log}" >&2
if ((status == 0)); then
  exit 1
fi
exit "${status}"
