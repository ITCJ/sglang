#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${EXP0_CONFIG:-${SCRIPT_DIR}/config.env}"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Missing config: ${CONFIG_FILE}" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"

: "${EXP0_MODEL_PATH:?EXP0_MODEL_PATH is required}"
: "${EXP0_MODEL_REVISION:?EXP0_MODEL_REVISION is required}"
: "${EXP0_SERVER_URL:?EXP0_SERVER_URL is required}"
: "${EXP0_MAX_TOTAL_TOKENS:?EXP0_MAX_TOTAL_TOKENS is required}"
: "${EXP0_HICACHE_SIZE_GB:?EXP0_HICACHE_SIZE_GB is required}"
: "${EXP0_MOONCAKE_CONFIG:?EXP0_MOONCAKE_CONFIG is required}"
: "${EXP0_MOONCAKE_STORE_CONFIG:?EXP0_MOONCAKE_STORE_CONFIG is required}"

for required_value in \
  "${EXP0_MODEL_PATH}" \
  "${EXP0_MODEL_REVISION}" \
  "${EXP0_MOONCAKE_CONFIG}" \
  "${EXP0_MOONCAKE_STORE_CONFIG}"; do
  if [[ "${required_value}" == *CHANGE_ME* ]]; then
    echo "Replace every CHANGE_ME value in ${CONFIG_FILE}" >&2
    exit 2
  fi
done

PYTHON_BIN="${EXP0_PYTHON:-python3}"

length="${1:-64k}"
runs="${2:-3}"
if [[ ! "${runs}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Runs must be a positive integer" >&2
  exit 2
fi
case "${length}" in
  64k)
    prefix_len=65408
    required_max_total_tokens=73728
    ;;
  128k)
    prefix_len=130944
    required_max_total_tokens=139264
    ;;
  *)
    echo "Length must be 64k or 128k" >&2
    exit 2
    ;;
esac

if [[ "${EXP0_MAX_TOTAL_TOKENS}" != "${required_max_total_tokens}" ]]; then
  echo "${length} requires EXP0_MAX_TOTAL_TOKENS=${required_max_total_tokens}" >&2
  exit 2
fi

results_root="${EXP0_RESULTS_DIR:-${SCRIPT_DIR}/results}/${length}"
manifest="${EXP0_MANIFEST:-${SCRIPT_DIR}/manifests/${length}.json}"
mkdir -p "${results_root}" "$(dirname -- "${manifest}")"

auth_args=()
if [[ -n "${EXP0_API_KEY:-}" ]]; then
  auth_args+=(--api-key "${EXP0_API_KEY}")
fi
if [[ -n "${EXP0_ADMIN_API_KEY:-}" ]]; then
  auth_args+=(--admin-api-key "${EXP0_ADMIN_API_KEY}")
fi

if [[ ! -f "${manifest}" ]]; then
  build_args=(
    build-manifest
    --tokenizer "${EXP0_TOKENIZER_PATH:-${EXP0_MODEL_PATH}}"
    --output "${manifest}"
    --prefix-len "${prefix_len}"
    --question-len 128
    --num-prefixes 10
    --seed 1
  )
  build_args+=(--revision "${EXP0_MODEL_REVISION}")
  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" "${build_args[@]}"
fi

"${SCRIPT_DIR}/collect_env.sh" "${results_root}/environment.txt"

server_started=0
cleanup() {
  if ((server_started)); then
    "${SCRIPT_DIR}/server_ctl.sh" stop || true
  fi
}
trap cleanup EXIT

"${SCRIPT_DIR}/server_ctl.sh" restart
server_started=1
server_log="$("${SCRIPT_DIR}/server_ctl.sh" log-path)"

set +e
"${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" preflight \
  --url "${EXP0_SERVER_URL}" \
  --manifest "${manifest}" \
  --server-log "${server_log}" \
  --mooncake-worker-config "${EXP0_MOONCAKE_CONFIG}" \
  --mooncake-store-config "${EXP0_MOONCAKE_STORE_CONFIG}" \
  --hicache-size-gb "${EXP0_HICACHE_SIZE_GB}" \
  --output "${results_root}/preflight.json" \
  "${auth_args[@]}"
preflight_rc=$?
set -e
if ((preflight_rc == 3)); then
  echo "Experiment skipped by the capacity guard; see preflight output." >&2
  exit 0
elif ((preflight_rc != 0)); then
  exit "${preflight_rc}"
fi

for run_number in $(seq 1 "${runs}"); do
  run_id="run-${run_number}"
  run_dir="${results_root}/${run_id}"
  mkdir -p "${run_dir}"

  if ((run_number > 1)); then
    "${SCRIPT_DIR}/server_ctl.sh" restart
    server_log="$("${SCRIPT_DIR}/server_ctl.sh" log-path)"
  fi

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" reset \
    --url "${EXP0_SERVER_URL}" "${auth_args[@]}"

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" l2 \
    --url "${EXP0_SERVER_URL}" \
    --manifest "${manifest}" \
    --server-log "${server_log}" \
    --mooncake-worker-config "${EXP0_MOONCAKE_CONFIG}" \
    --mooncake-store-config "${EXP0_MOONCAKE_STORE_CONFIG}" \
    --output "${run_dir}/l2.jsonl" \
    --run-id "${run_id}" \
    --length "${length}" \
    --hicache-size-gb "${EXP0_HICACHE_SIZE_GB}" \
    "${auth_args[@]}"
  cp -- "${server_log}" "${run_dir}/server-l2.log"

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" wait \
    --url "${EXP0_SERVER_URL}" "${auth_args[@]}"
  "${SCRIPT_DIR}/server_ctl.sh" restart
  server_log="$("${SCRIPT_DIR}/server_ctl.sh" log-path)"

  "${PYTHON_BIN}" "${SCRIPT_DIR}/exp0.py" l3 \
    --url "${EXP0_SERVER_URL}" \
    --manifest "${manifest}" \
    --server-log "${server_log}" \
    --mooncake-worker-config "${EXP0_MOONCAKE_CONFIG}" \
    --mooncake-store-config "${EXP0_MOONCAKE_STORE_CONFIG}" \
    --output "${run_dir}/l3.jsonl" \
    --run-id "${run_id}" \
    --length "${length}" \
    --hicache-size-gb "${EXP0_HICACHE_SIZE_GB}" \
    "${auth_args[@]}"
  cp -- "${server_log}" "${run_dir}/server-l3.log"
done

"${PYTHON_BIN}" "${SCRIPT_DIR}/summarize.py" "${results_root}" \
  --expected-runs "${runs}" \
  --json-output "${results_root}/summary.json" \
  --markdown-output "${results_root}/summary.md"

"${SCRIPT_DIR}/server_ctl.sh" stop
server_started=0
echo "Experiment complete: ${results_root}"
