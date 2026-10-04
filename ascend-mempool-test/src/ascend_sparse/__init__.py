"""Load sparse KV configuration without importing the SGLang serving stack."""

from pathlib import Path

__path__.append(
    str(
        Path(__file__).resolve().parents[3]
        / "python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload"
    )
)
