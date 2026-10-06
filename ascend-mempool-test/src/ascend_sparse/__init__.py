"""Load sparse KV configuration without importing the SGLang serving stack."""

from ascend_npu import NPU_PATH

__path__.append(str(NPU_PATH / "sparsity_driven_kv_offload"))
