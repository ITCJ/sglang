#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_PATH="${1:?Usage: $0 OUTPUT_PATH}"

mkdir -p "$(dirname -- "${OUTPUT_PATH}")"
: >"${OUTPUT_PATH}"

run_optional() {
  local title="$1"
  shift
  {
    printf '\n## %s\n' "${title}"
    printf '$'
    printf ' %q' "$@"
    printf '\n'
    if command -v "$1" >/dev/null 2>&1; then
      if "$@"; then
        printf '[exit=0]\n'
      else
        printf '[exit=%d]\n' "$?"
      fi
    else
      printf '[unavailable: %s]\n' "$1"
    fi
  } >>"${OUTPUT_PATH}" 2>&1
}

run_optional "UTC time" date -u --iso-8601=seconds
run_optional "Git commit" git -C "${REPO_ROOT}" rev-parse HEAD
run_optional "Git branch" git -C "${REPO_ROOT}" branch --show-current
run_optional "Kernel" uname -a
run_optional "CPU topology" lscpu
run_optional "NUMA topology" numactl --hardware
run_optional "Memory" lsmem --summary=only
run_optional "PCI devices" lspci -nn
run_optional "Network links" ip -details link show
run_optional "RDMA links" rdma link show
run_optional "InfiniBand devices" ibv_devices
run_optional "InfiniBand netdev mapping" ibdev2netdev
run_optional "Ascend devices" npu-smi info
run_optional "Ascend topology" npu-smi info -t topo

printf 'Environment capture written to %s\n' "${OUTPUT_PATH}"
