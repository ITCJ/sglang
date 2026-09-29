#!/usr/bin/env bash
# Run on an A3 host: create a new container and initialize the three-path bench.
set -euo pipefail

setup_inside() {
    local setup_log_dir=$1
    local stage=environment
    trap 'rc=$?; printf "{\"status\":\"%s\",\"stage\":\"%s\",\"exit_code\":%s}\n" "$([[ $rc == 0 ]] && echo ok || echo failed)" "$stage" "$rc" > "$setup_log_dir/initialize-status.json"' EXIT
    [[ -e /.dockerenv || -e /run/.containerenv ]] || { echo 'Container initialization must run inside a container.'; exit 1; }
    [[ $(uname -m) == aarch64 ]] || { echo 'Requires A3 aarch64.'; exit 1; }

    # The same environment is loaded by future docker exec ... bash -l sessions.
    local cann_env=
    for candidate in "${ASCEND_HOME_PATH:-/nonexistent}/set_env.sh" \
                     /usr/local/Ascend/ascend-toolkit/set_env.sh \
                     /usr/local/Ascend/ascend-toolkit/latest/set_env.sh \
                     /usr/local/Ascend/cann/set_env.sh \
                     /usr/local/Ascend/cann-*/set_env.sh; do
        if [[ -f $candidate ]]; then cann_env=$candidate; break; fi
    done
    [[ -n $cann_env ]] || { echo 'Missing CANN set_env.sh in this image.'; exit 1; }
    {
        printf 'set +u\nsource %q\n' "$cann_env"
        printf 'export SOC_VERSION=Ascend910_9382\nexport ASCEND_SOC_VERSION=Ascend910_9382\n'
    } > /etc/profile.d/unidex-bench.sh
    # CANN's vendor script may reference unset optional variables.
    set +u
    source /etc/profile.d/unidex-bench.sh
    set -u
    command -v python3 >/dev/null
    python3 "$UNIDEX_REPO/workspace/unidex_copy_bench/capture_environment.py" \
        --output "$setup_log_dir/environment-before.json" --image-digest "$UNIDEX_IMAGE_DIGEST"

    stage=kernel-api
    # Import/API checks only: no tensors, device initialization or transfers.
    if python3 - <<'PY'
import inspect
import torch
import torch_npu
from sgl_kernel_npu.sparsity_driven_kv_offload import create_shm_tensor, free_shm, unidex_copy_inplace
assert 'src_ptr' in inspect.signature(unidex_copy_inplace).parameters
for name in ('unidex_copy', 'shm_allocator_create_and_register', 'shm_allocator_free_all'):
    assert hasattr(torch.ops.npu, name), f'missing native operator {name}'
print('Existing image has the required UNIDEX APIs; retaining its kernel package.')
PY
    then
        printf 'KERNEL_ACTION=retain\n'
    else
        stage=kernel-compatibility
        python3 - "$setup_log_dir/environment-before.json" <<'PY'
import json
import sys
from importlib.metadata import version
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
assert sys.version_info[:2] == (3, 11), 'Fallback wheel requires Python 3.11; retain this image and choose a matching kernel build.'
for package in ('torch', 'torch-npu'):
    assert version(package).split('+')[0] == '2.10.0', f'Fallback wheel requires {package} 2.10.0'
cann = record.get('cann')
assert isinstance(cann, dict) and any(line.split('=', 1)[-1].strip() == '9.0.0' for line in cann['version_lines']), 'Fallback wheel requires verified CANN 9.0.0'
PY
        stage=kernel-download-install
        # Verified official release asset. Only its sgl_kernel_npu wheel is installed.
        python3 - "$setup_log_dir" <<'PY'
import hashlib
from email.parser import Parser
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
from zipfile import ZipFile
root = Path(sys.argv[1])
name = 'sgl-kernel-npu-2026.9.0-torch2.10.0-py311-cann9.0.0-a3-aarch64.zip'
url = 'https://github.com/sgl-project/sgl-kernel-npu/releases/download/2026.9.0/' + name
expected = '68372ac96c328fabdc0b7c9c0cc19c6540fbe775ad35ab7ab83e9446029dcfc4'
archive = root / name
print('Downloading fixed official kernel archive', flush=True)
with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as output:
    while chunk := response.read(1024 * 1024):
        output.write(chunk)
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != expected:
    raise RuntimeError('Official archive SHA256 mismatch; no wheel installed')
with ZipFile(archive) as bundle:
    wheels = [entry for entry in bundle.namelist() if Path(entry).name.startswith('sgl_kernel_npu-') and entry.endswith('.whl')]
    if len(wheels) != 1:
        raise RuntimeError('Expected exactly one sgl_kernel_npu wheel')
    wheel = root / Path(wheels[0]).name
    wheel.write_bytes(bundle.read(wheels[0]))
with ZipFile(wheel) as package:
    entries = [name for name in package.namelist() if name.endswith('.dist-info/METADATA')]
    if len(entries) != 1:
        raise RuntimeError('Invalid wheel metadata')
    info = Parser().parsestr(package.read(entries[0]).decode())
    if info['Name'].replace('_', '-').lower() != 'sgl-kernel-npu' or info['Version'] != '2026.9.0':
        raise RuntimeError('Unexpected kernel wheel identity')
    if 'sgl_kernel_npu/sparsity_driven_kv_offload/ops.py' not in package.namelist():
        raise RuntimeError('Release wheel lacks the UNIDEX Python module')
(root / 'kernel-install.json').write_text(json.dumps(dict(url=url, archive_sha256=actual,
    wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
    version=info['Version']), indent=2) + '\n')
subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps', '--force-reinstall', str(wheel)], check=True)
PY
    fi

    stage=memfabric
    if python3 - <<'PY'
import memfabric_hybrid
from memfabric_hybrid import bm
for name in ('initialize', 'uninitialize', 'create2', 'BmConfig'):
    assert hasattr(bm, name), name
for enum, names in ((bm.BmMemType, ('HOST', 'DEVICE', 'LOCAL_DEVICE')),
                    (bm.BmCopyType, ('H2GH', 'GH2L', 'G2G')),
                    (bm.BmDataOpType, ('SDMA',))):
    for name in names:
        assert hasattr(enum, name), name
assert hasattr(bm.BmConfig(), 'set_nic')
handles = [cls for cls in vars(bm).values() if isinstance(cls, type)
           and hasattr(cls, 'peer_rank_ptr') and hasattr(cls, 'copy_data_batch')]
assert any(hasattr(cls, 'gva_to_va') for cls in handles), 'BM handle type lacks gva_to_va mapping API'
print('Existing MemFabric APIs available; retaining installed package.')
PY
    then
        printf 'MEMFABRIC_ACTION=retain\n'
    else
        python3 -m pip install --no-deps --disable-pip-version-check --timeout 60 --retries 2 \
            --force-reinstall 'memfabric-hybrid==1.1.5'
    fi

    stage=final-api-check
    python3 - <<'PY'
import inspect
from importlib.metadata import version
import torch
import torch_npu
import memfabric_hybrid as mf
from memfabric_hybrid import bm
from sgl_kernel_npu.sparsity_driven_kv_offload import create_shm_tensor, free_shm, unidex_copy_inplace
assert 'src_ptr' in inspect.signature(unidex_copy_inplace).parameters
for name in ('unidex_copy', 'shm_allocator_create_and_register', 'shm_allocator_free_all'):
    assert hasattr(torch.ops.npu, name), name
for name in ('initialize', 'uninitialize'):
    assert hasattr(mf, name), name
for name in ('initialize', 'uninitialize', 'create2', 'BmConfig'):
    assert hasattr(bm, name), name
for enum, names in ((bm.BmMemType, ('HOST', 'DEVICE', 'LOCAL_DEVICE')),
                    (bm.BmCopyType, ('H2GH', 'GH2L', 'G2G')),
                    (bm.BmDataOpType, ('SDMA',))):
    for name in names:
        assert hasattr(enum, name), name
assert hasattr(bm.BmConfig(), 'set_nic')
handles = [cls for cls in vars(bm).values() if isinstance(cls, type)
           and hasattr(cls, 'peer_rank_ptr') and hasattr(cls, 'copy_data_batch')]
assert any(hasattr(cls, 'gva_to_va') for cls in handles), 'BM handle type lacks gva_to_va mapping API'
for name in ('torch', 'torch-npu', 'sgl-kernel-npu', 'memfabric-hybrid'):
    print(f'{name}={version(name)}')
print('UNIDEX_IMPORT=' + inspect.getfile(unidex_copy_inplace))
print('API checks only; no NPU allocation, correctness check or benchmark performed.')
PY
    stage=record
    python3 "$UNIDEX_REPO/workspace/unidex_copy_bench/capture_environment.py" \
        --output "$setup_log_dir/environment-after.json" --image-digest "$UNIDEX_IMAGE_DIGEST"
    stage=complete
    printf 'UNIDEX_ENV_READY\n'
    # EXIT trap uses the function's local stage/log variables.
    exit 0
}

if [[ ${1:-} == --inside ]]; then
    [[ $# == 2 ]] || exit 2
    setup_inside "$2"
    exit
fi

if [[ $# -lt 1 || $# -gt 2 || ${1:-} == --help ]]; then
    echo 'Usage: bash workspace/setup/start_unidex_container.sh <LOCAL_IMAGE_NAME_OR_ID> [NEW_CONTAINER_NAME]'
    echo 'Creates a new A3 experiment container, initializes installed packages, and opens a shell if interactive.'
    exit 2
fi
for tool in docker python3 git; do command -v "$tool" >/dev/null; done
[[ $(uname -m) == aarch64 ]] || { echo 'Run this script on the target A3 aarch64 host.'; exit 1; }
[[ -d /usr/local/Ascend/driver ]] || { echo 'Missing host Ascend driver.'; exit 1; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/../.." && pwd)
image_ref=$1
stamp=$(date +%Y%m%d-%H%M%S)-$$
container_name=${2:-unidex-kv-$stamp}
[[ $container_name =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo 'Invalid container name.'; exit 2; }
if docker container inspect "$container_name" >/dev/null 2>&1; then
    echo 'Container name already exists; select a new name. Existing container retained.'
    exit 1
fi
# Never silently pull a different image; use the image chosen by the operator.
image_id=$(docker image inspect --format '{{.Id}}' "$image_ref")
[[ $(docker image inspect --format '{{.Architecture}}' "$image_id") == arm64 ]] || { echo 'Requires an arm64 image.'; exit 1; }
log_dir="$repo_dir/workspace/unidex_copy_bench/logs/container-$stamp"
mkdir -p "$log_dir"
interactive=0
if [[ -t 0 && -t 1 ]]; then interactive=1; fi
exec > >(tee "$log_dir/setup.log") 2>&1
stage=create
trap 'rc=$?; printf "{\"stage\":\"%s\",\"exit_code\":%s}\n" "$stage" "$rc" > "$log_dir/setup-status.json"; if [[ $rc != 0 ]]; then printf "SETUP_FAILED stage=%s log=%s/setup.log; container retained if created.\n" "$stage" "$log_dir"; fi' EXIT
printf 'LOG_DIR=%s\nCONTAINER=%s\n' "$log_dir" "$container_name"
printf '%s\n' "$container_name" > "$log_dir/container-name.txt"
git -C "$repo_dir" rev-parse HEAD > "$log_dir/repo-commit.txt"
docker image inspect --format '{{json .RepoDigests}}' "$image_id" > "$log_dir/image-repodigests.json"
image_digest=$(python3 -c 'import json,sys; items=json.load(open(sys.argv[1])); print(items[0] if items else "unknown")' "$log_dir/image-repodigests.json")
printf '%s\n' "$image_id" > "$log_dir/image-id.txt"
args=(run -d --init --name "$container_name" --network host --privileged
      --shm-size=32g --ulimit memlock=-1:-1 --entrypoint /bin/bash
      -e "UNIDEX_REPO=$repo_dir" -e "UNIDEX_IMAGE_DIGEST=$image_digest"
      -e SOC_VERSION=Ascend910_9382 -e ASCEND_SOC_VERSION=Ascend910_9382
      -v "$repo_dir:$repo_dir" -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro
      -w "$repo_dir")
for device in /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc; do
    [[ ! -e $device ]] || args+=(--device "$device")
done
for path in /etc/localtime /etc/hccn.conf /etc/ascend_install.info /usr/local/sbin/npu-smi /sys/class/infiniband; do
    [[ ! -e $path ]] || args+=(-v "$path:$path:ro")
done
[[ ! -e /dev/infiniband ]] || args+=(-v /dev/infiniband:/dev/infiniband)
args+=("$image_id" -c 'exec sleep infinity')
printf 'DOCKER_COMMAND='; printf ' %q' docker "${args[@]}"; printf '\n'
docker "${args[@]}" > "$log_dir/container-id.txt"
stage=initialize
docker exec "$container_name" bash "$repo_dir/workspace/setup/start_unidex_container.sh" --inside "$log_dir"
stage=complete
printf 'Container initialized. Enter with: docker exec -it %q bash -l\n' "$container_name"
printf 'Stop after experiments: docker stop %q\n' "$container_name"
# Preserve a successful setup result independently of the interactive shell exit.
printf '{"stage":"complete","exit_code":0}\n' > "$log_dir/setup-status.json"
trap - EXIT
if [[ $interactive == 1 ]]; then
    # Restore the terminal descriptor before starting an interactive docker exec.
    docker exec -it "$container_name" bash -l > /dev/tty 2>&1
fi
