#!/usr/bin/env bash
# One command per existing NPU container; owns only its own worker processes.
set -eo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ " $* " != *" --describe "* && " $* " != *" --help "* ]]; then
    source /usr/local/Ascend/ascend-toolkit/set_env.sh
    source /usr/local/Ascend/nnal/atb/set_env.sh
    export LD_LIBRARY_PATH="/usr/local/Ascend/ascend-toolkit/latest/opp/vendors/customize/op_api/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export PATH="/usr/local/Ascend/8.5.0/compiler/bishengir/bin:$PATH"
fi
exec python3 -u "$script_dir/run_numa_cd.py" "$@"
