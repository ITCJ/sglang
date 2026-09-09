#!/usr/bin/env bash
set -uo pipefail

# Run inside the same Ascend environment used by SGLang.
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "python: FAIL (${PYTHON_BIN} not found)"
  exit 2
fi

os_name="unknown"
if [[ -r /etc/os-release ]]; then
  os_name="$(. /etc/os-release; echo "${PRETTY_NAME:-unknown}")"
fi
glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null || echo unknown)"
python_version="$("${PYTHON_BIN}" -c 'import platform; print(platform.python_version())')"
machine="$(uname -m)"
echo "system: ${os_name}; ${glibc_version}; ${machine}; Python ${python_version}"

cann_version="not found"
for version_file in \
  /usr/local/Ascend/ascend-toolkit/latest/version.cfg \
  /usr/local/Ascend/driver/version.info \
  /etc/ascend_install.info; do
  if [[ -r "${version_file}" ]]; then
    version_line="$(grep -Eim1 '(^|_)(version|version_dir)=' "${version_file}" || true)"
    cann_version="${version_line:-present at ${version_file}}"
    break
  fi
done
echo "Ascend/CANN: ${cann_version}; hccn.conf=$([[ -r /etc/hccn.conf ]] && echo yes || echo no)"

if command -v npu-smi >/dev/null 2>&1; then
  npu_output="$(timeout 10s npu-smi info 2>&1 || true)"
  npu_models="$(
    echo "${npu_output}" | grep -Eo 'Ascend[[:space:]]*[0-9A-Za-z-]+|9(10|50)[0-9A-Za-z-]*' |
      sort -u | paste -sd, -
  )"
  echo "NPU: npu-smi OK; model=${npu_models:-not parsed}"
else
  echo "NPU: npu-smi not found"
fi

package_version="$(
  "${PYTHON_BIN}" -c \
    'from importlib.metadata import version; print(version("mooncake-transfer-engine-npu"))' \
    2>/dev/null || echo not-installed
)"
echo "Mooncake wheel: ${package_version}"

for library in libibverbs.so.1 librdmacm.so.1; do
  if "${PYTHON_BIN}" -c \
    "import ctypes; ctypes.CDLL(\"${library}\")" >/dev/null 2>&1; then
    echo "${library}: OK"
  else
    echo "${library}: MISSING"
  fi
done

mooncake_import="$("${PYTHON_BIN}" - <<'PY'
try:
    import mooncake.engine
    from mooncake.store import MooncakeDistributedStore
    import mooncake.mooncake_store_service
    print("OK")
except BaseException as exc:
    message = str(exc).replace("\n", " ")
    print(f"FAIL ({type(exc).__name__}: {message[:180]})")
PY
)"
echo "Mooncake import: ${mooncake_import}"

if command -v mooncake_master >/dev/null 2>&1; then
  echo "mooncake_master: OK ($(command -v mooncake_master))"
else
  echo "mooncake_master: NOT FOUND"
fi

shopt -s nullglob
rdma_paths=(/sys/class/infiniband/*)
rdma_ports=()
for device_path in "${rdma_paths[@]}"; do
  for port_path in "${device_path}"/ports/*; do
    device="$(basename "${device_path}")"
    port="$(basename "${port_path}")"
    state="$(cat "${port_path}/state" 2>/dev/null || echo unknown)"
    state="${state#*: }"
    link_layer="$(cat "${port_path}/link_layer" 2>/dev/null || echo unknown)"
    rdma_ports+=("${device}/p${port}:${state}/${link_layer}")
  done
done
if ((${#rdma_ports[@]})); then
  rdma_description="$(IFS=,; echo "${rdma_ports[*]}")"
  echo "RDMA kernel ports: ${rdma_description}"
else
  echo "RDMA kernel ports: none"
fi

rdma_userspace=no
if command -v ibv_devinfo >/dev/null 2>&1 && \
  ibv_devinfo -l 2>/dev/null | grep -Eq '[1-9][0-9]* HCAs? found|hca_id:'; then
  rdma_userspace=yes
fi
echo "RDMA Verbs userspace device: ${rdma_userspace}"

hixl_found=no
if command -v ldconfig >/dev/null 2>&1 && \
  ldconfig -p 2>/dev/null | grep -Eqi 'hixl|adxl'; then
  hixl_found=yes
elif [[ -d /usr/local/Ascend ]] && [[ -n "$(
  find /usr/local/Ascend -maxdepth 7 -type f \
    \( -iname '*hixl*.so*' -o -iname '*adxl*.so*' \) \
    -print -quit 2>/dev/null
)" ]]; then
  hixl_found=yes
fi
echo "HIXL/ADXL library clue: ${hixl_found}"
echo "Fabric env: ASCEND_ENABLE_USE_FABRIC_MEM=${ASCEND_ENABLE_USE_FABRIC_MEM-<unset>}; HCCL_INTRA_ROCE_ENABLE=${HCCL_INTRA_ROCE_ENABLE-<unset>}"

if [[ "${mooncake_import}" != "OK" ]]; then
  echo "RESULT: fix Mooncake/shared-library loading first."
elif [[ "${rdma_userspace}" == "yes" ]]; then
  echo "RESULT: protocol=rdma is a candidate; Ethernet link layer means RoCE."
elif [[ "${hixl_found}" == "yes" ]]; then
  echo "RESULT: evaluate protocol=ascend/Fabric Memory against the exact NPU and CANN/HDK versions."
else
  echo "RESULT: no high-speed transport is proven; use TCP only for a smoke test."
fi
