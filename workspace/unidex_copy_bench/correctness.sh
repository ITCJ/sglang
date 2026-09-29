#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 4 ]]; then
    printf 'Usage: bash correctness.sh <NEW_LOG_DIR> <SOURCE_DIR> [<DEVICE>] [<IMAGE_DIGEST>]\n' >&2
    exit 2
fi
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECK_DIR=$1
SOURCE_DIR=$2
DEVICE=${3:-0}
IMAGE_DIGEST=${4:-unknown}
CHECK_PYTHON=$(python3 -c 'import sys; print(sys.executable)')
[[ ! -e "$CHECK_DIR" ]] || { printf 'Choose a new log directory.\n' >&2; exit 2; }
mkdir -p "$CHECK_DIR"
CHECK_DIR=$(cd "$CHECK_DIR" && pwd)
exec > >(tee "$CHECK_DIR/cli.log") 2>&1
stage=environment
finish_check() {
    local rc=$?
    "$CHECK_PYTHON" - "$CHECK_DIR/correctness-status.json" "$stage" "$rc" <<'PY'
import json
from pathlib import Path
import sys
Path(sys.argv[1]).write_text(json.dumps(dict(status="ok" if sys.argv[3] == "0" else "failed",
                                           stage=sys.argv[2], exit_code=int(sys.argv[3]),
                                           validation_requested=True,
                                           error=None if sys.argv[3] == "0" else "see cli.log and per-case log/JSON")) + "\n")
PY
    printf 'CORRECTNESS_EXIT stage=%s code=%s\n' "$stage" "$rc"
}
trap finish_check EXIT
printf 'CORRECTNESS_COMMAND'; printf ' %q' bash "$0" "$@"; printf '\n'
printf 'CORRECTNESS_CWD=%s\n' "$PWD"
"$CHECK_PYTHON" "$SCRIPT_DIR/capture_environment.py" --output "$CHECK_DIR/environment.json" \
    --kernel-source-dir "$SOURCE_DIR" --image-digest "$IMAGE_DIGEST"
for specification in '128 contiguous' '4096 scattered'; do
    read -r tokens layout <<< "$specification"
    stage="${tokens}-${layout}"
    timeout --kill-after=30s 1800s "$CHECK_PYTHON" "$SCRIPT_DIR/../kv_path_bench/kv_transfer_bench.py" \
        --copy-engine unidex --l2-only --device "$DEVICE" --block-dim 24 \
        --tokens "$tokens" --layout "$layout" --warmup 0 --repeats 1 --validate \
        --output "$CHECK_DIR/$stage.json" --log "$CHECK_DIR/$stage.log"
done
printf 'CORRECTNESS_OK: 128 contiguous and 4096 scattered; byte and guard validation enabled\n'
