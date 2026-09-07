#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 2 ]]; then
  echo "Usage: bash workspace/docker_run_sglang_ascend.sh <code_home_host> <model_home_host>" >&2
  echo "Example: bash workspace/docker_run_sglang_ascend.sh /home1path /home2path" >&2
  exit 2
fi

CODE_HOME_HOST="$1"
MODEL_HOME_HOST="$2"
CODE_REPO_IN_CONTAINER="${CODE_HOME_HOST%/}/sglang"

IMAGE="${IMAGE:-quay.io/ascend/sglang:cann9.0.0-a3-v0.5.16}"
CONTAINER_NAME="${CONTAINER_NAME:-sglang_ascend}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -d "${CODE_HOME_HOST}" ]]; then
  echo "Missing code home on host: ${CODE_HOME_HOST}" >&2
  exit 2
fi
if [[ ! -d "${MODEL_HOME_HOST}" ]]; then
  echo "Missing model home on host: ${MODEL_HOME_HOST}" >&2
  exit 2
fi
if [[ ! -d "${CODE_HOME_HOST%/}/sglang/python/sglang" ]]; then
  echo "Expected SGLang repo at ${CODE_HOME_HOST%/}/sglang" >&2
  exit 2
fi

DOCKER_ARGS=(
  run -it
  --shm-size=16g
  --ulimit memlock=-1:-1
  --name "${CONTAINER_NAME}"
  --net=host
  --privileged
  --entrypoint /bin/bash
  -e "HOST_SGLANG_REPO=${CODE_REPO_IN_CONTAINER}"
  -e "MODEL_HOME=${MODEL_HOME_HOST%/}"
  -e "PYTHON_BIN=${PYTHON_BIN}"
  -v /etc/localtime:/etc/localtime:ro
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver
  -v /var/log/npu:/usr/slog
  -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi:ro
  -v /sys/fs/cgroup:/sys/fs/cgroup:ro
  -v "${CODE_HOME_HOST}:${CODE_HOME_HOST}"
)

if [[ "${MODEL_HOME_HOST}" != "${CODE_HOME_HOST}" ]]; then
  DOCKER_ARGS+=( -v "${MODEL_HOME_HOST}:${MODEL_HOME_HOST}" )
fi

for dev in /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc; do
  [[ -e "${dev}" ]] && DOCKER_ARGS+=( --device="${dev}" )
done

for path in \
  /etc/hccn.conf \
  /etc/ascend_install.info \
  /etc/default/grub \
  /dev/infiniband \
  /sys/class/infiniband; do
  if [[ -e "${path}" ]]; then
    mode=ro
    [[ "${path}" == "/dev/infiniband" ]] && mode=rw
    DOCKER_ARGS+=( -v "${path}:${path}:${mode}" )
  fi
done

# RDMA userspace libraries/providers are installed inside the container with
# install_rdma_runtime.sh. Host library mounts would block package installation.
CONTAINER_SETUP='set -euo pipefail
repo="${HOST_SGLANG_REPO}"
if [[ ! -d "${repo}/python/sglang" ]]; then
  echo "Missing mounted repo package: ${repo}/python/sglang" >&2
  exit 2
fi

"${PYTHON_BIN}" - <<'"'"'PY'"'"'
import importlib.util
import os
import pathlib
import shutil
import sysconfig

repo = pathlib.Path(os.environ["HOST_SGLANG_REPO"])
src = repo / "python" / "sglang"
spec = importlib.util.find_spec("sglang")
if spec and spec.submodule_search_locations:
    dst = pathlib.Path(next(iter(spec.submodule_search_locations)))
else:
    dst = pathlib.Path(sysconfig.get_paths()["purelib"]) / "sglang"

if dst.is_symlink():
    if pathlib.Path(os.readlink(dst)) == src:
        print(f"SGLang source already linked: {dst} -> {src}")
        raise SystemExit(0)
    dst.unlink()
elif dst.exists():
    shutil.rmtree(dst)

dst.parent.mkdir(parents=True, exist_ok=True)
os.symlink(src, dst, target_is_directory=True)
print(f"SGLang source linked: {dst} -> {src}")
PY

cd "${repo}"
echo "Container ready. Repo=${repo}; MODEL_HOME=${MODEL_HOME}"
exec /bin/bash
'

DOCKER_ARGS+=( -w "${CODE_REPO_IN_CONTAINER}" "${IMAGE}" -lc "${CONTAINER_SETUP}" )

exec docker "${DOCKER_ARGS[@]}"
