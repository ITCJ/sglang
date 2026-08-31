#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "== Fix RDMA soname links inside the container =="

can_load() {
  local soname="$1"
  "${PYTHON_BIN}" - <<PY >/dev/null 2>&1
import ctypes
ctypes.CDLL("${soname}")
PY
}

ensure_soname() {
  local soname="$1"
  local candidate=""
  local dir=""
  local link=""

  if can_load "${soname}"; then
    echo "${soname}: already loadable"
    return 0
  fi

  for dir in /usr/lib64 /usr/lib/aarch64-linux-gnu /lib/aarch64-linux-gnu /usr/lib /lib; do
    candidate="$(find "${dir}" -maxdepth 1 -type f -name "${soname}.*" -print -quit 2>/dev/null || true)"
    if [[ -n "${candidate}" ]]; then
      link="$(dirname "${candidate}")/${soname}"
      if [[ ! -e "${link}" ]]; then
        ln -s "$(basename "${candidate}")" "${link}"
        echo "${soname}: linked ${link} -> $(basename "${candidate}")"
      else
        echo "${soname}: link already exists at ${link}"
      fi
      return 0
    fi
  done

  echo "${soname}: no versioned library found" >&2
  return 1
}

ensure_soname libibverbs.so.1
ensure_soname librdmacm.so.1

command -v ldconfig >/dev/null 2>&1 && ldconfig || true

for soname in libibverbs.so.1 librdmacm.so.1; do
  if can_load "${soname}"; then
    echo "${soname}: loadable"
  else
    echo "${soname}: still missing" >&2
    exit 1
  fi
done

"${PYTHON_BIN}" - <<'PY'
try:
    import mooncake.engine
    from mooncake.store import MooncakeDistributedStore
    print("Mooncake import: OK")
except BaseException as exc:
    print(f"Mooncake import: FAIL {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
    raise SystemExit(1)
PY

echo "RESULT: RDMA soname links fixed for this container."
