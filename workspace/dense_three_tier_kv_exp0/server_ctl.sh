#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${EXP0_CONFIG:-${SCRIPT_DIR}/config.env}"
RUNTIME_DIR="${EXP0_RUNTIME_DIR:-${SCRIPT_DIR}/runtime}"
PID_FILE="${RUNTIME_DIR}/server.pid"
CURRENT_LOG_FILE="${RUNTIME_DIR}/current_log"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Missing config: ${CONFIG_FILE}" >&2
  echo "Start from ${SCRIPT_DIR}/config.env.example" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"

: "${EXP0_MODEL_PATH:?EXP0_MODEL_PATH is required}"
: "${EXP0_MODEL_REVISION:?EXP0_MODEL_REVISION is required}"
: "${EXP0_SERVER_URL:?EXP0_SERVER_URL is required}"
: "${EXP0_HOST:?EXP0_HOST is required}"
: "${EXP0_PORT:?EXP0_PORT is required}"
: "${EXP0_MAX_TOTAL_TOKENS:?EXP0_MAX_TOTAL_TOKENS is required}"
: "${EXP0_HICACHE_SIZE_GB:?EXP0_HICACHE_SIZE_GB is required}"
: "${EXP0_MOONCAKE_CONFIG:?EXP0_MOONCAKE_CONFIG is required}"

for required_value in \
  "${EXP0_MODEL_PATH}" \
  "${EXP0_MODEL_REVISION}" \
  "${EXP0_MOONCAKE_CONFIG}"; do
  if [[ "${required_value}" == *CHANGE_ME* ]]; then
    echo "Replace every CHANGE_ME value in ${CONFIG_FILE}" >&2
    exit 2
  fi
done

PYTHON_BIN="${EXP0_PYTHON:-python3}"

mkdir -p "${RUNTIME_DIR}"

read_pid() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(<"${PID_FILE}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${pid}"
}

is_running() {
  local pid
  pid="$(read_pid)" || return 1
  kill -0 "${pid}" 2>/dev/null
}

validate_server_pid() {
  local pid="$1"
  local command_line
  command_line="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
  [[ "${command_line}" == *"sglang.launch_server"* ]]
}

auth_args=()
if [[ -n "${EXP0_API_KEY:-}" ]]; then
  auth_args=(-H "Authorization: Bearer ${EXP0_API_KEY}")
fi

start_server() {
  if is_running; then
    echo "Server already running with PID $(read_pid)" >&2
    return 1
  fi
  if [[ ! -f "${EXP0_MOONCAKE_CONFIG}" ]]; then
    echo "Missing Mooncake worker config: ${EXP0_MOONCAKE_CONFIG}" >&2
    return 2
  fi

  local stamp log_file pid
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log_file="${RUNTIME_DIR}/server.${stamp}.log"
  printf '%s\n' "${log_file}" >"${CURRENT_LOG_FILE}"

  local command=(
    "${PYTHON_BIN}" -m sglang.launch_server
    --model-path "${EXP0_MODEL_PATH}"
    --host "${EXP0_HOST}"
    --port "${EXP0_PORT}"
    --tp-size 16
    --dp-size 16
    --enable-dp-attention
    --dcp-size 1
    --attention-backend ascend
    --kv-cache-dtype bfloat16
    --page-size 64
    --max-running-requests 128
    --max-total-tokens "${EXP0_MAX_TOTAL_TOKENS}"
    --enable-hierarchical-cache
    --hicache-size "${EXP0_HICACHE_SIZE_GB}"
    --hicache-write-policy write_through
    --hicache-io-backend kernel_ascend
    --hicache-mem-layout page_first_kv_split
    --hicache-storage-backend mooncake
    --hicache-storage-prefetch-policy wait_complete
    --enable-metrics
    --enable-cache-report
  )
  if [[ -n "${EXP0_MODEL_REVISION:-}" ]]; then
    command+=(--revision "${EXP0_MODEL_REVISION}")
  fi
  if [[ -n "${EXP0_API_KEY:-}" ]]; then
    command+=(--api-key "${EXP0_API_KEY}")
  fi
  if [[ -n "${EXP0_ADMIN_API_KEY:-}" ]]; then
    command+=(--admin-api-key "${EXP0_ADMIN_API_KEY}")
  fi
  if declare -p EXP0_EXTRA_SERVER_ARGS >/dev/null 2>&1; then
    if [[ "$(declare -p EXP0_EXTRA_SERVER_ARGS)" != "declare -a"* ]]; then
      echo "EXP0_EXTRA_SERVER_ARGS must be a Bash array" >&2
      return 2
    fi
    local argument flag
    for argument in "${EXP0_EXTRA_SERVER_ARGS[@]}"; do
      flag="${argument%%=*}"
      case "${flag}" in
        --model|--model-path|--revision|--host|--port|--tp|--tp-size|--dp|--dp-size|--enable-dp-attention|--disable-dp-attention|--dcp|--dcp-size|--attention-backend|--kv-cache-dtype|--page-size|--max-running-requests|--max-total-tokens|--enable-hierarchical-cache|--disable-hierarchical-cache|--hicache-*|--enable-metrics|--disable-metrics|--enable-cache-report|--disable-cache-report|--api-key|--admin-api-key)
          echo "EXP0_EXTRA_SERVER_ARGS cannot override fixed flag ${flag}" >&2
          return 2
          ;;
      esac
    done
    command+=("${EXP0_EXTRA_SERVER_ARGS[@]}")
  fi

  (
    cd "${REPO_ROOT}"
    export PYTHONPATH="${REPO_ROOT}/python${PYTHONPATH:+:${PYTHONPATH}}"
    export SGLANG_HICACHE_MOONCAKE_CONFIG_PATH="${EXP0_MOONCAKE_CONFIG}"
    exec setsid "${command[@]}"
  ) >>"${log_file}" 2>&1 &
  pid=$!
  printf '%s\n' "${pid}" >"${PID_FILE}"
  echo "Started server PID ${pid}; log: ${log_file}"

  local deadline=$((SECONDS + 1800))
  until curl -fsS "${auth_args[@]}" "${EXP0_SERVER_URL}/health" >/dev/null 2>&1; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      rm -f -- "${PID_FILE}"
      echo "Server exited during startup; see ${log_file}" >&2
      return 1
    fi
    if ((SECONDS >= deadline)); then
      echo "Timed out waiting for ${EXP0_SERVER_URL}/health" >&2
      stop_server || true
      return 1
    fi
    sleep 5
  done
  echo "Server ready at ${EXP0_SERVER_URL}"
}

stop_server() {
  local pid
  pid="$(read_pid)" || {
    echo "No server PID file"
    return 0
  }
  if ! kill -0 "${pid}" 2>/dev/null; then
    rm -f -- "${PID_FILE}"
    return 0
  fi
  if ! validate_server_pid "${pid}"; then
    echo "Refusing to stop PID ${pid}: command is not sglang.launch_server" >&2
    return 2
  fi

  kill -TERM -- "-${pid}"
  local deadline=$((SECONDS + 180))
  while kill -0 "${pid}" 2>/dev/null; do
    if ((SECONDS >= deadline)); then
      echo "Server did not stop after 180 seconds; sending SIGKILL" >&2
      kill -KILL -- "-${pid}"
      break
    fi
    sleep 2
  done
  rm -f -- "${PID_FILE}"
}

case "${1:-}" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  restart)
    stop_server
    start_server
    ;;
  status)
    if is_running; then
      echo "running PID $(read_pid)"
    else
      echo "stopped"
      exit 1
    fi
    ;;
  log-path)
    [[ -f "${CURRENT_LOG_FILE}" ]] || {
      echo "No current log" >&2
      exit 1
    }
    cat "${CURRENT_LOG_FILE}"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log-path}" >&2
    exit 2
    ;;
esac
