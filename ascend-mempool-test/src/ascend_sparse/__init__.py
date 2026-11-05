"""Load sparse KV configuration without importing the SGLang serving stack."""

from ascend_npu import NPU_PATH

__path__.append(str(NPU_PATH / "sparsity_driven_kv_offload"))

# Keep the shared backend identical in production and standalone fixtures.
import importlib
import sys

sys.modules.setdefault(
    "sglang.srt.hardware_backend.npu.mempool.multibase",
    importlib.import_module("ascend_mempool.multibase"),
)
