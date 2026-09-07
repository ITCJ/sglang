#!/usr/bin/env bash
# Ubuntu runtime packages for Mooncake RDMA. Run as root inside the container.
# Uses the configured Ubuntu mirror; does not install drivers or rebuild Mooncake.
# References: Mooncake docs/source/getting_started/quick-start.md and
# scripts/ascend/dependencies_ascend_installation.sh (runtime subset only).
set -euo pipefail

if [[ "${EUID}" != 0 ]]; then
  echo 'R1:ROOT'
  exit 2
fi
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 22.04 ]] ||
   [[ "$(dpkg --print-architecture)" != arm64 ]]; then
  echo 'R1:OS'
  exit 2
fi
if [[ ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
  echo 'R1:CONTAINER'
  exit 2
fi

log="$(mktemp /tmp/mooncake-rdma-install-XXXXXX.log)"
echo "Installation log: ${log}"

stage() {
  echo "$1" | tee -a "${log}"
}

# dpkg cannot safely replace individual files bind-mounted from the host.
if awk '$5 ~ /\/lib(ibverbs|rdmacm|nl-3|nl-route-3)\.so/ {found=1; print $5}
        END {exit !found}' /proc/self/mountinfo >"${log}"; then
  echo 'R1:MOUNT'
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt_options=(-o Acquire::Retries=0 -o Acquire::http::Timeout=20
             -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=10)
stage '[1/4] Updating Ubuntu package lists...'
if ! apt-get "${apt_options[@]}" -o APT::Update::Error-Mode=any update 2>&1 | tee -a "${log}"; then
  echo 'R1:REPO'
  exit 1
fi

# apt resolves libnl and other transitive dependencies from the same distro.
stage '[2/4] Installing RDMA runtime packages...'
if ! apt-get "${apt_options[@]}" install -y --no-install-recommends \
    libibverbs1 ibverbs-providers librdmacm1 rdma-core ibverbs-utils \
    libnl-3-200 libnl-route-3-200 2>&1 | tee -a "${log}"; then
  echo 'R1:INSTALL'
  exit 1
fi
stage '[3/4] Refreshing the shared-library cache...'
if ! ldconfig 2>&1 | tee -a "${log}"; then
  echo 'R1:LOADER'
  exit 1
fi

stage '[4/4] Checking the installed environment...'
bash "${script_dir}/diagnose_mooncake.sh" 2>&1 | tee -a "${log}"
