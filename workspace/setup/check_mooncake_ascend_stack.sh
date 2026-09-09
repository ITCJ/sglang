#!/usr/bin/env bash
set -uo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "== Mooncake Ascend stack check =="
echo "system: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}"); $(uname -m); glibc $(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"

cann_latest="/usr/local/Ascend/ascend-toolkit/latest"
echo "cann_latest: $(readlink -f "${cann_latest}" 2>/dev/null || echo missing)"
for f in "${cann_latest}/version.cfg" /usr/local/Ascend/driver/version.info /etc/ascend_install.info; do
  [[ -r "$f" ]] && { echo "version_file: $f: $(grep -Eim1 'version|Version|version_dir' "$f" || echo present)"; break; }
done

if command -v npu-smi >/dev/null 2>&1; then
  npu_line="$(timeout 10s npu-smi info 2>/dev/null | grep -Eim1 '910|A3|Chip|Name|Type' || true)"
  echo "npu_smi: OK${npu_line:+; ${npu_line}}"
else
  echo "npu_smi: missing"
fi

pkg="$(${PYTHON_BIN} - <<'PY' 2>/dev/null || true
from importlib.metadata import version, PackageNotFoundError
try:
    print(version('mooncake-transfer-engine-npu'))
except PackageNotFoundError:
    print('not-installed')
PY
)"
echo "mooncake_npu_wheel: ${pkg:-unknown}"

for so in libibverbs.so.1 librdmacm.so.1; do
  if "${PYTHON_BIN}" - <<PY >/dev/null 2>&1
import ctypes
ctypes.CDLL('${so}')
PY
  then
    echo "${so}: loadable"
  else
    found="$(find /usr /usr/local /opt -name "${so}*" -print -quit 2>/dev/null || true)"
    echo "${so}: missing${found:+; found_not_in_loader=${found}}"
  fi
done

python_import="$(${PYTHON_BIN} - <<'PY' 2>&1
try:
    import mooncake.engine
    from mooncake.store import MooncakeDistributedStore
    print('OK')
except BaseException as e:
    print(f'FAIL {type(e).__name__}: {str(e).splitlines()[0][:160]}')
PY
)"
echo "mooncake_import: ${python_import}"

hixl="no"
if find /usr/local/Ascend /usr /opt -maxdepth 8 -type f \( -iname '*hixl*.so*' -o -iname '*adxl*.so*' \) -print -quit 2>/dev/null | grep -q .; then
  hixl="yes"
fi
echo "hixl_adxl_lib: ${hixl}"

echo "fabric_env: ASCEND_ENABLE_USE_FABRIC_MEM=${ASCEND_ENABLE_USE_FABRIC_MEM-<unset>}; HCCL_INTRA_ROCE_ENABLE=${HCCL_INTRA_ROCE_ENABLE-<unset>}"

if [[ "${python_import}" != OK ]]; then
  echo "RESULT: first fix Mooncake wheel shared-library loading; do not blindly apt-get on managed Huawei images."
elif [[ "${hixl}" == yes ]]; then
  echo "RESULT: ready to run protocol=ascend + Fabric Memory Mooncake Store put/get test."
else
  echo "RESULT: Mooncake imports, but Fabric Memory library clue is missing; ask platform owner for Ascend/HIXL stack."
fi
