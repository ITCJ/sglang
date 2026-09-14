#!/usr/bin/env bash
set -euo pipefail

# Inside BOTH A3 containers: install BM APIs for fabric_direct_bench Host -> NPU.
# Replaces the current MemFabric package; does not initialize an NPU.
LOG=/tmp/a3-memfabric-install.log
exec 3>&1
exec >"${LOG}" 2>&1
trap 'echo "MF1" >&3' ERR

python3 -m pip install --upgrade 'memfabric-hybrid==1.1.5'
python3 - <<'PY'
from importlib.metadata import version
import memfabric_hybrid as mf
from memfabric_hybrid import bm

assert version("memfabric-hybrid") == "1.1.5"
for name in ("initialize", "uninitialize", "set_log_level"):
    assert hasattr(mf, name), f"missing mf.{name}"
for name in ("initialize", "uninitialize", "create2", "BmConfig"):
    assert hasattr(bm, name), f"missing bm.{name}"
for enum, names in ((bm.BmCopyType, ("H2GH", "GH2L")),
                    (bm.BmMemType, ("HOST", "DEVICE")),
                    (bm.BmDataOpType, ("SDMA",))):
    for name in names:
        assert hasattr(enum, name), f"missing enum member {name}"
assert hasattr(bm.BmConfig(), "set_nic"), "missing BmConfig.set_nic"
print("MemFabric 1.1.5 import and BM API check passed; no transfer tested.")
PY
echo 'MF0' >&3
