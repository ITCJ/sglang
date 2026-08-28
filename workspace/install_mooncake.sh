#!/usr/bin/env bash
set -euo pipefail

# Run this after activating the same Ascend Python environment used by SGLang.
PYTHON_BIN="python3"
MOONCAKE_VERSION="0.3.12.post1"

"${PYTHON_BIN}" -m pip install \
  "mooncake-transfer-engine-npu==${MOONCAKE_VERSION}"

"${PYTHON_BIN}" -c '
from importlib.metadata import version

import mooncake.engine
from mooncake.store import MooncakeDistributedStore
import mooncake.mooncake_store_service

package_name = "mooncake-transfer-engine-npu"
print(f"Mooncake Transfer Engine + Store: OK ({version(package_name)})")
'

if ! command -v mooncake_master >/dev/null 2>&1; then
  echo "mooncake_master was installed but is not on PATH." >&2
  echo "Activate the Python environment again, then rerun this script." >&2
  exit 1
fi

echo "mooncake_master: $(command -v mooncake_master)"
