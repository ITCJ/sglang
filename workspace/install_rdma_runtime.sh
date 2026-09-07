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

# dpkg cannot safely replace individual files bind-mounted from the host.
if awk '$5 ~ /\/lib(ibverbs|rdmacm|nl-3|nl-route-3)\.so/ {found=1; print $5}
        END {exit !found}' /proc/self/mountinfo >"${log}"; then
  echo 'R1:MOUNT'
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bash "${script_dir}/check_network.sh" --require apt

export DEBIAN_FRONTEND=noninteractive
apt_options=(-o Acquire::Retries=0 -o Acquire::http::Timeout=20
             -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=10)
if ! apt-get "${apt_options[@]}" -o APT::Update::Error-Mode=any update >>"${log}" 2>&1; then
  echo 'R1:REPO'
  exit 1
fi

# apt resolves libnl and other transitive dependencies from the same distro.
if ! apt-get "${apt_options[@]}" install -y --no-install-recommends \
    libibverbs1 ibverbs-providers librdmacm1 rdma-core ibverbs-utils \
    libnl-3-200 libnl-route-3-200 >>"${log}" 2>&1; then
  echo 'R1:INSTALL'
  exit 1
fi
if ! ldconfig >>"${log}" 2>&1; then
  echo 'R1:LOADER'
  exit 1
fi

bash "${script_dir}/diagnose_mooncake.sh"
