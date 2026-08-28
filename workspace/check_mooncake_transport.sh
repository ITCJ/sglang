#!/usr/bin/env bash
set -uo pipefail

# Run inside the same Ascend environment used by SGLang.
PYTHON_BIN="${PYTHON_BIN:-python3}"

section() {
  printf '\n== %s ==\n' "$1"
}

run_if_present() {
  local command_name="$1"
  shift
  if command -v "${command_name}" >/dev/null 2>&1; then
    echo "+ ${command_name} $*"
    timeout 15s "${command_name}" "$@" 2>&1 || \
      echo "[command exited with status $?]"
  else
    echo "${command_name}: not installed"
  fi
}

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python executable not found: ${PYTHON_BIN}" >&2
  exit 2
fi

section "System"
uname -a
if [[ -r /etc/os-release ]]; then
  sed -n '1,12p' /etc/os-release
fi
getconf GNU_LIBC_VERSION 2>&1 || true
"${PYTHON_BIN}" --version
echo "python: $(command -v "${PYTHON_BIN}")"
echo "hostname: $(hostname -f 2>/dev/null || hostname)"
hostname -I 2>/dev/null || true

section "Ascend"
run_if_present npu-smi info
ascend_version_found=0
for version_file in \
  /usr/local/Ascend/ascend-toolkit/latest/version.cfg \
  /usr/local/Ascend/driver/version.info \
  /etc/ascend_install.info; do
  if [[ -r "${version_file}" ]]; then
    ascend_version_found=1
    echo "--- ${version_file}"
    sed -n '1,80p' "${version_file}"
  fi
done
if ((ascend_version_found == 0)); then
  echo "Ascend version files: not found"
fi
if [[ -r /etc/hccn.conf ]]; then
  echo "/etc/hccn.conf: present"
else
  echo "/etc/hccn.conf: missing or unreadable"
fi
for name in \
  ASCEND_HOME_PATH \
  LD_LIBRARY_PATH \
  ASCEND_ENABLE_USE_FABRIC_MEM \
  ASCEND_USE_ASYNC_TRANSFER \
  ASCEND_GLOBAL_RESOURCE_CONFIG \
  HCCL_INTRA_ROCE_ENABLE; do
  printf '%s=%s\n' "${name}" "${!name-<unset>}"
done

section "Mooncake package and shared libraries"
"${PYTHON_BIN}" - <<'PY'
import ctypes
import importlib
import subprocess
from importlib.metadata import PackageNotFoundError, distribution, version

package_name = "mooncake-transfer-engine-npu"
try:
    dist = distribution(package_name)
    print(f"{package_name}={version(package_name)}")
except PackageNotFoundError:
    print(f"{package_name}: not installed")
    dist = None

for library in ("libibverbs.so.1", "librdmacm.so.1"):
    try:
        ctypes.CDLL(library)
        print(f"{library}: load OK")
    except OSError as exc:
        print(f"{library}: LOAD FAILED: {exc}")

if dist is not None:
    shared_objects = sorted(
        str(dist.locate_file(path))
        for path in (dist.files or [])
        if ".so" in path.name
    )
    print(f"Mooncake shared objects: {len(shared_objects)}")
    for path in shared_objects:
        result = subprocess.run(
            ["ldd", path], text=True, capture_output=True, check=False
        )
        missing = [line.strip() for line in result.stdout.splitlines() if "not found" in line]
        if missing:
            print(f"Missing dependencies for {path}:")
            for line in missing:
                print(f"  {line}")

for module_name in (
    "mooncake.engine",
    "mooncake.store",
    "mooncake.mooncake_store_service",
):
    try:
        importlib.import_module(module_name)
        print(f"import {module_name}: OK")
    except BaseException as exc:
        print(f"import {module_name}: FAILED: {type(exc).__name__}: {exc}")
PY

if command -v mooncake_master >/dev/null 2>&1; then
  master_path="$(command -v mooncake_master)"
  echo "mooncake_master: ${master_path}"
  missing_master_libs="$(ldd "${master_path}" 2>/dev/null | awk '/not found/')"
  if [[ -n "${missing_master_libs}" ]]; then
    echo "Missing mooncake_master dependencies:"
    echo "${missing_master_libs}"
  else
    echo "mooncake_master shared libraries: OK"
  fi
else
  echo "mooncake_master: not found on PATH"
fi

section "Host RDMA / RoCE"
shopt -s nullglob
rdma_devices=(/sys/class/infiniband/*)
if ((${#rdma_devices[@]})); then
  echo "Kernel RDMA devices:"
  for device_path in "${rdma_devices[@]}"; do
    device_name="$(basename "${device_path}")"
    echo "  ${device_name}"
    for port_path in "${device_path}"/ports/*; do
      port="$(basename "${port_path}")"
      state="$(cat "${port_path}/state" 2>/dev/null || echo unknown)"
      link_layer="$(cat "${port_path}/link_layer" 2>/dev/null || echo unknown)"
      echo "    port ${port}: state=${state}; link_layer=${link_layer}"
    done
  done
else
  echo "Kernel RDMA devices: none under /sys/class/infiniband"
fi
run_if_present ibv_devices
run_if_present ibv_devinfo
run_if_present rdma link show

section "HIXL / Memory Fabric clues"
hixl_found=0
if command -v ldconfig >/dev/null 2>&1; then
  hixl_ldconfig="$(
    ldconfig -p 2>/dev/null | awk 'BEGIN {IGNORECASE=1} /hixl|adxl/ {print}'
  )"
  if [[ -n "${hixl_ldconfig}" ]]; then
    hixl_found=1
    echo "${hixl_ldconfig}"
  fi
fi
if [[ -d /usr/local/Ascend ]]; then
  hixl_files="$(
    find /usr/local/Ascend -maxdepth 7 -type f \
      \( -iname '*hixl*.so*' -o -iname '*adxl*.so*' \) \
      -print 2>/dev/null | head -50
  )"
  if [[ -n "${hixl_files}" ]]; then
    hixl_found=1
    echo "${hixl_files}"
  fi
fi
if ((hixl_found == 0)); then
  echo "No HIXL/ADXL shared-library clues found."
fi

section "Summary"
if "${PYTHON_BIN}" -c 'import ctypes; ctypes.CDLL("libibverbs.so.1")' \
  >/dev/null 2>&1; then
  echo "[OK] libibverbs.so.1 is loadable."
else
  echo "[FAIL] libibverbs.so.1 is not loadable; fix the system RDMA runtime first."
fi

if "${PYTHON_BIN}" -c \
  'import mooncake.engine; from mooncake.store import MooncakeDistributedStore' \
  >/dev/null 2>&1; then
  echo "[OK] Mooncake Transfer Engine and Store import successfully."
else
  echo "[FAIL] Mooncake import failed; inspect the missing libraries above."
fi

if command -v ibv_devinfo >/dev/null 2>&1 && \
  ibv_devinfo -l 2>/dev/null | grep -Eq '[1-9][0-9]* HCAs? found|hca_id:'; then
  echo "[OK] Host RDMA device(s) are visible through the Verbs API."
  echo "     protocol=rdma is a candidate; Ethernet link_layer means RoCE."
elif ((${#rdma_devices[@]})); then
  echo "[WARN] Kernel RDMA objects exist, but ibv_devinfo cannot query a device."
  echo "       Check container device mounts, permissions, drivers, and rdma-core."
else
  echo "[WARN] No Host RDMA device is exposed; protocol=rdma cannot be assumed."
  echo "       Use TCP only for a smoke test, or validate Ascend/Fabric transport."
fi

echo "[INFO] Memory Fabric needs A3 hardware plus matching CANN/HDK/HIXL support."
echo "       Files or environment variables alone do not prove it is operational."
