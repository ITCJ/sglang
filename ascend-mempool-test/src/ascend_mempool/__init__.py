"""Test adapters that load NPU mempool code without SGLang service imports."""

from pathlib import Path

# Load the runtime modules as ascend_mempool.* to bypass sglang's public API.
__path__.append(
    str(
        Path(__file__).resolve().parents[3]
        / "python/sglang/srt/hardware_backend/npu/mempool"
    )
)
