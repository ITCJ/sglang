#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
    printf 'Usage: bash install.sh <SOURCE_DIR> <NEW_LOG_DIR> [<IMAGE_DIGEST>]\n' >&2
    exit 2
fi
SOURCE_COMMIT=d9261669b0303a28369d07c0eea0bd1627235dd6
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE_DIR=$1
INSTALL_LOG_DIR=$2
IMAGE_DIGEST=${3:-unknown}
INSTALL_PYTHON=$(python3 -c 'import sys; print(sys.executable)')
if [[ -e "$INSTALL_LOG_DIR" ]]; then
    printf 'Log directory already exists; choose a new directory.\n' >&2
    exit 2
fi
mkdir -p "$INSTALL_LOG_DIR"
INSTALL_LOG_DIR=$(cd "$INSTALL_LOG_DIR" && pwd)
exec > >(tee "$INSTALL_LOG_DIR/install.log") 2>&1
stage=environment
finish_install() {
    local rc=$?
    "$INSTALL_PYTHON" - "$INSTALL_LOG_DIR/install-status.json" "$stage" "$rc" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps(dict(status="ok" if sys.argv[3] == "0" else "failed",
                                           stage=sys.argv[2], exit_code=int(sys.argv[3]),
                                           error=None if sys.argv[3] == "0" else "see install.log/source-verification.log",
                                           python_executable=sys.executable)) + "\n")
PY
    printf 'INSTALL_EXIT stage=%s code=%s\n' "$stage" "$rc"
}
trap finish_install EXIT
printf 'INSTALL_COMMAND'; printf ' %q' bash "$0" "$@"; printf '\n'
printf 'INSTALL_CWD=%s\n' "$PWD"
printf 'INSTALL_PYTHON=%s\n' "$INSTALL_PYTHON"
stage=source-directory
SOURCE_DIR=$(cd "$SOURCE_DIR" && pwd)
stage=environment
"$INSTALL_PYTHON" "$SCRIPT_DIR/capture_environment.py" --output "$INSTALL_LOG_DIR/environment-before.json" \
    --kernel-source-dir "$SOURCE_DIR" --image-digest "$IMAGE_DIGEST"
stage=manifest
[[ $(cat "$SOURCE_DIR/unidex-source-manifest/source_commit.txt") == "$SOURCE_COMMIT" ]]
[[ -s "$SOURCE_DIR/unidex-source-manifest/submodules.txt" ]]
cp "$SOURCE_DIR/unidex-source-manifest/source_commit.txt" "$INSTALL_LOG_DIR/source_commit.txt"
cp "$SOURCE_DIR/unidex-source-manifest/submodules.txt" "$INSTALL_LOG_DIR/submodules.txt"
(
    cd "$SOURCE_DIR"
    sha256sum --check unidex-source-manifest/source-files.sha256
) > "$INSTALL_LOG_DIR/source-verification.log" 2>&1
stage=dependencies
if [[ $(uname -m) != aarch64 ]]; then
    printf 'Requires target aarch64 A3 environment.\n' >&2
    exit 1
fi
for tool in cmake g++ make sha256sum; do
    command -v "$tool" >/dev/null || { printf 'Missing tool: %s; prepare it offline.\n' "$tool" >&2; exit 1; }
done
stage=headers
CANN_ROOT=${ASCEND_HOME_PATH:-/usr/local/Ascend/ascend-toolkit/latest}
if [[ -f "$CANN_ROOT" && $(basename "$CANN_ROOT") == set_env.sh ]]; then
    CANN_ROOT=$(dirname "$CANN_ROOT")
fi
CANN_ROOT=$(readlink -f "$CANN_ROOT") || { printf 'Cannot resolve CANN root; specify ASCEND_HOME_PATH.\n' >&2; exit 1; }
[[ -f "$CANN_ROOT/set_env.sh" ]] || { printf 'Missing CANN set_env.sh under %s\n' "$CANN_ROOT" >&2; exit 1; }
export ASCEND_HOME_PATH="$CANN_ROOT"
export ASCEND_TOOLKIT_HOME="$CANN_ROOT"
CANN_INCLUDE="$CANN_ROOT/aarch64-linux/include"
[[ -f "$CANN_INCLUDE/acl/acl.h" ]] || { printf 'Missing CANN header: %s/acl/acl.h\n' "$CANN_INCLUDE" >&2; exit 1; }
HAL_HEADER_FOUND=0
for include in "$CANN_INCLUDE" "$CANN_INCLUDE/external" \
    "$CANN_INCLUDE/experiment/platform" "$CANN_INCLUDE/experiment/runtime" \
    /usr/local/Ascend/driver/include /usr/local/Ascend/driver/kernel/inc; do
    if [[ -f "$include/driver/ascend_hal_define.h" ]]; then
        printf 'HAL_HEADER=%s/driver/ascend_hal_define.h\n' "$include"
        HAL_HEADER_FOUND=1
    fi
done
[[ $HAL_HEADER_FOUND == 1 ]] || { printf 'Missing driver/ascend_hal_define.h in upstream include roots.\n' >&2; exit 1; }
ASCENDC_FOUND=0
for directory in "$CANN_ROOT/tools/tikcpp/ascendc_kernel_cmake" \
    "$CANN_ROOT/compiler/tikcpp/ascendc_kernel_cmake" "$CANN_ROOT/ascendc_devkit/tikcpp/samples/cmake"; do
    [[ ! -f "$directory/ascendc.cmake" ]] || ASCENDC_FOUND=1
done
[[ $ASCENDC_FOUND == 1 ]] || { printf 'Missing CANN ascendc.cmake; install matching development headers/tools offline.\n' >&2; exit 1; }
stage=python-dependencies
"$INSTALL_PYTHON" - <<'PY'
from importlib import metadata
import sys
required = ("wheel", "setuptools", "pybind11", "pip", "torch", "torch-npu")
for package in required:
    try:
        version = metadata.version(package)
    except metadata.PackageNotFoundError:
        sys.exit(f"Missing {package}; prepare matching aarch64 dependencies offline. See README.")
    print(f"DEPENDENCY {package}={version}")
    if package == "wheel" and version != "0.45.1":
        sys.exit("Requires wheel==0.45.1; prepare it offline. See README.")
import torch
import torch_npu
print(f"TORCH_CXX11_ABI={torch.compiled_with_cxx11_abi()}")
PY
"$INSTALL_PYTHON" - "$SOURCE_DIR" "$INSTALL_LOG_DIR/package-version.txt" <<'PY'
from configparser import ConfigParser
from pathlib import Path
import sys
source = Path(sys.argv[1])
config = ConfigParser()
config.read(source / "config.ini")
version = config.get("global", "version")
print(f"SOURCE_PACKAGE_VERSION={version}")
Path(sys.argv[2]).write_text(version + "\n")
PY
# Upstream removes build/; require a fresh extracted source tree first.
for directory in "$SOURCE_DIR/build" "$SOURCE_DIR/output"; do
    [[ ! -e "$directory" ]] || { printf 'Use a fresh extracted source tree; existing %s retained.\n' "$directory" >&2; exit 1; }
done
export PIP_NO_INDEX=1
export PIP_NO_DEPS=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export BUILD_CATLASS_MODULE=OFF
export SOC_VERSION=Ascend910_9382
export ASCEND_SOC_VERSION=Ascend910_9382
stage=offline-build-entry
"$INSTALL_PYTHON" "$SCRIPT_DIR/prepare_offline_build.py" "$SOURCE_DIR" "$INSTALL_LOG_DIR"
stage=build
touch "$INSTALL_LOG_DIR/build-start.marker"
(
    cd "$SOURCE_DIR"
    bash build.sh -a kernels Ascend910_9382
)
stage=wheel-selection
mapfile -d '' -t BUILT_WHEELS < <(find "$SOURCE_DIR/output" -maxdepth 1 -type f \
    -name 'sgl_kernel_npu*.whl' -newer "$INSTALL_LOG_DIR/build-start.marker" -print0)
if [[ ${#BUILT_WHEELS[@]} -ne 1 ]]; then
    printf 'Expected exactly one newly built kernel wheel, found %s.\n' "${#BUILT_WHEELS[@]}" >&2
    exit 1
fi
sha256sum "${BUILT_WHEELS[0]}" > "$INSTALL_LOG_DIR/installed-wheel.sha256"
"$INSTALL_PYTHON" - "${BUILT_WHEELS[0]}" "$INSTALL_LOG_DIR/package-version.txt" <<'PY'
from email.parser import Parser
from pathlib import Path
import sys
from zipfile import ZipFile
with ZipFile(sys.argv[1]) as wheel:
    entries = [name for name in wheel.namelist() if name.endswith('.dist-info/METADATA')]
    if len(entries) != 1:
        sys.exit("Expected one wheel METADATA")
    record = Parser().parsestr(wheel.read(entries[0]).decode())
    if record['Name'].replace('_', '-').lower() != 'sgl-kernel-npu':
        sys.exit("Unexpected wheel package")
    if record['Version'] != Path(sys.argv[2]).read_text().strip():
        sys.exit("Wheel version differs from verified source config")
    print(f"BUILT_WHEEL_VERSION={record['Version']}")
PY
stage=install
"$INSTALL_PYTHON" -m pip install --no-index --no-deps --force-reinstall "${BUILT_WHEELS[0]}"
stage=environment-after
"$INSTALL_PYTHON" "$SCRIPT_DIR/capture_environment.py" --output "$INSTALL_LOG_DIR/environment-after.json" \
    --kernel-source-dir "$SOURCE_DIR" --image-digest "$IMAGE_DIGEST"
printf 'INSTALL_OK commit=%s wheel=%s\n' "$SOURCE_COMMIT" "${BUILT_WHEELS[0]}"
