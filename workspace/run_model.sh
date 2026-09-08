#!/usr/bin/env bash
# Diagnostic model smoke test: one A3 node, TP16+DP1, no HiCache yet.
set -euo pipefail
if [[ $# != 1 ]]; then
  echo 'Usage: bash workspace/run_model.sh <model-directory>' >&2
  exit 2
fi
MODEL_PATH="$1"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVER_PORT="${SERVER_PORT:-30000}"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}/python${PYTHONPATH:+:${PYTHONPATH}}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export PYTHONUNBUFFERED=1

# Check shard presence before touching NPU resources. This is not a checksum
# check: run only after the model copy has completed.
"${PYTHON_BIN}" - "${MODEL_PATH}" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
if not (root / 'config.json').is_file():
    raise SystemExit('Missing model config.json; check the container-visible path.')
config = json.loads((root / 'config.json').read_text())
print('Model:', root, 'architecture:', config.get('architectures'))
if config.get('model_type') != 'deepseek_v3':
    raise SystemExit('Expected DeepSeek V3 model_type; inspect config before launch.')
indexes = list(root.glob('*.safetensors.index.json'))
if not indexes:
    raise SystemExit('Missing safetensors shard index; inspect the model export.')
for index in indexes:
    names = set(json.loads(index.read_text()).get('weight_map', {}).values())
    if not names:
        raise SystemExit('Empty weight_map in ' + index.name)
    missing = [name for name in names if not (root / name).is_file() or (root / name).stat().st_size == 0]
    if missing:
        raise SystemExit('Missing/empty weight shards: ' + ', '.join(sorted(missing)[:8]))
    print('Weight shard presence OK:', len(names))
print('Presence checks do not prove transfer completion or weight integrity.')
PY

export SGLANG_SET_CPU_AFFINITY="${SGLANG_SET_CPU_AFFINITY:-1}"
export SGLANG_NPU_PROFILING=0
export PYTORCH_NPU_ALLOC_CONF="${PYTORCH_NPU_ALLOC_CONF:-expandable_segments:True}"
export STREAMS_PER_DEVICE="${STREAMS_PER_DEVICE:-32}"
export HCCL_BUFFSIZE="${HCCL_BUFFSIZE:-1600}"
export HCCL_OP_EXPANSION_MODE="${HCCL_OP_EXPANSION_MODE:-AIV}"
unset ASCEND_LAUNCH_BLOCKING

"${PYTHON_BIN}" - <<'PY'
import torch
import torch_npu
print('torch:', torch.__version__, 'torch_npu:', torch_npu.__version__)
count = torch.npu.device_count()
print('Visible logical NPUs:', count)
if count < 16:
    raise SystemExit('TP16 requires at least 16 visible logical NPUs.')
PY

LOG_DIR="$(mktemp -d /tmp/sglang-model-XXXXXX)"
echo "Server log: ${LOG_DIR}/server.log"
echo "Starting model smoke test on ${SERVER_HOST}:${SERVER_PORT}; Ctrl+C stops it."
echo 'This uses a small token budget and eager execution, not experiment performance settings.'

# A foreground pipeline retains live logs; pipefail preserves launch failures.
"${PYTHON_BIN}" -m sglang.launch_server \
  --model-path "${MODEL_PATH}" \
  --host "${SERVER_HOST}" --port "${SERVER_PORT}" \
  --device npu --trust-remote-code --watchdog-timeout 9000 \
  --quantization modelslim --dtype bfloat16 --kv-cache-dtype bfloat16 \
  --tp-size 16 --dp-size 1 --enable-dp-attention --enable-dp-lm-head \
  --dcp-size 1 --attention-backend ascend --page-size 64 \
  --max-running-requests 16 --max-total-tokens 8192 \
  --context-length 4096 --chunked-prefill-size 4096 \
  --disable-cuda-graph --enable-metrics --enable-cache-report \
  2>&1 | tee "${LOG_DIR}/server.log"
