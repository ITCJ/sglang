"""Test adapters that load NPU mempool code without SGLang service imports."""

from ascend_npu import NPU_PATH

# Load the runtime modules as ascend_mempool.* to bypass sglang's public API.
__path__.append(str(NPU_PATH / "mempool"))
