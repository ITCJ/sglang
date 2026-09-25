"""Configuration and validation for sparsity-driven KV offload."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Optional

from sglang.srt.configs.model_config import (
    get_dsa_index_head_dim,
    get_dsa_index_topk,
    is_deepseek_dsa,
)
from sglang.srt.environ import envs
from sglang.srt.runtime_context import (
    attention_backends,
    get_disagg,
    get_schedule,
    process_model_config,
    uses_mla_backend,
)
from sglang.srt.utils.common import is_npu

if TYPE_CHECKING:
    from sglang.srt.configs.model_config import ModelConfig


class SparseKVOffloadMode(str, Enum):
    DISABLED = "disabled"
    LOCAL_OFFLOAD = "local_offload"
    PD_PREFILL_NATIVE = "pd_prefill_native"
    PD_DECODE_OFFLOAD = "pd_decode_offload"

    @property
    def uses_host_kv_offload(self) -> bool:
        return self in (
            SparseKVOffloadMode.LOCAL_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
        )

    @property
    def uses_pd_decode_staging(self) -> bool:
        return self is SparseKVOffloadMode.PD_DECODE_OFFLOAD


def resolve_sparse_kv_offload_mode(
    *,
    model_config: Optional[ModelConfig] = None,
    use_mla_backend: Optional[bool] = None,
) -> SparseKVOffloadMode:
    if not envs.SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD.get():
        return SparseKVOffloadMode.DISABLED

    # The NPU MLA pool has no ModelConfig argument; use the published process
    # configuration there. Callers holding a model config pass it explicitly.
    if model_config is None:
        model_config = process_model_config()
    if use_mla_backend is None:
        use_mla_backend = uses_mla_backend()

    prefill_attention_backend, decode_attention_backend = attention_backends()
    if not (
        is_npu()
        and prefill_attention_backend == "ascend"
        and decode_attention_backend == "ascend"
        and use_mla_backend
        and is_deepseek_dsa(model_config.hf_config)
    ):
        raise ValueError(
            "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD requires an NPU "
            "DSA-family MLA model "
            "(for example DeepSeek V3.2 or GLM-5.x) using the Ascend MLA "
            "attention backend."
        )
    if get_schedule().max_running_requests is None:
        raise ValueError(
            "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD requires max_running_requests "
            "to be set to bound the per-process host KV allocation."
        )

    disagg = get_disagg()
    if disagg.disaggregation_mode == "null":
        return SparseKVOffloadMode.LOCAL_OFFLOAD
    if disagg.disaggregation_mode in ("prefill", "decode"):
        if disagg.disaggregation_transfer_backend != "ascend":
            raise ValueError(
                "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD with PD disaggregation "
                "requires disaggregation_transfer_backend='ascend'; got "
                f"{disagg.disaggregation_transfer_backend!r}."
            )
        if disagg.disaggregation_mode == "prefill":
            return SparseKVOffloadMode.PD_PREFILL_NATIVE
        return SparseKVOffloadMode.PD_DECODE_OFFLOAD
    raise ValueError(
        "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD received unsupported "
        f"disaggregation_mode={disagg.disaggregation_mode!r}."
    )


def get_sparsity_driven_kv_offload_sparse_context_len(
    *,
    model_config: ModelConfig,
) -> int:
    """Return the per-request on-device sparse KV window size."""
    sparse_context_len = int(get_dsa_index_topk(model_config.hf_config))
    if sparse_context_len <= 0:
        raise ValueError(
            "Sparsity-driven KV offload requires a positive DSA index_topk, "
            f"got {sparse_context_len}."
        )
    return sparse_context_len


def get_sparsity_driven_kv_offload_index_head_dim(
    *,
    model_config: ModelConfig,
) -> int:
    index_head_dim = getattr(model_config, "index_head_dim", None)
    if index_head_dim is None:
        index_head_dim = get_dsa_index_head_dim(model_config.hf_config)
    index_head_dim = int(index_head_dim)
    if index_head_dim <= 0:
        raise ValueError(
            "Sparsity-driven KV offload requires a positive DSA index_head_dim, "
            f"got {index_head_dim}."
        )
    return index_head_dim


def get_sparsity_driven_kv_offload_cell_size(
    *,
    model_config: ModelConfig,
    use_mla_backend: bool,
    num_layers: int,
    element_size: int,
) -> Optional[int]:
    mode = resolve_sparse_kv_offload_mode(
        model_config=model_config,
        use_mla_backend=use_mla_backend,
    )
    if not mode.uses_host_kv_offload:
        return None

    index_head_dim = get_sparsity_driven_kv_offload_index_head_dim(
        model_config=model_config
    )
    return index_head_dim * num_layers * element_size
