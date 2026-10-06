"""Load pure NPU helpers without importing the SGLang serving stack."""

import importlib
import sys
from pathlib import Path

NPU_PATH = (
    Path(__file__).resolve().parents[3] / "python/sglang/srt/hardware_backend/npu"
)
__path__.append(str(NPU_PATH))
sys.modules.setdefault(
    "sglang.srt.hardware_backend.npu.kv_rows",
    importlib.import_module("ascend_npu.kv_rows"),
)
