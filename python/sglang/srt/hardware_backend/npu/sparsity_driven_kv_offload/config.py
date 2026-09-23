"""Configuration and validation for sparsity-driven KV offload."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from sglang.srt.configs.model_config import (
    get_dsa_index_head_dim,
    get_dsa_index_topk,
    is_deepseek_dsa,
)
from sglang.srt.environ import envs
from sglang.srt.utils.common import is_npu

if TYPE_CHECKING:
    from sglang.srt.configs.model_config import ModelConfig
    from sglang.srt.server_args import ServerArgs


SPARSE_KV_DEVICE_CACHE_WINDOW_COUNT = 2


def is_sparsity_driven_kv_offload_enabled(
    *,
    model_config: ModelConfig,
    server_args: ServerArgs,
    use_mla_backend: bool,
) -> bool:
    if not envs.SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD.get():
        return False

    if not (
        is_npu()
        and server_args.attention_backend == "ascend"
        and use_mla_backend
        and is_deepseek_dsa(model_config.hf_config)
    ):
        raise ValueError(
            "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD requires an NPU "
            "DSA-family MLA model "
            "(for example DeepSeek V3.2 or GLM-5.x) using the Ascend MLA "
            "attention backend."
        )
    if server_args.max_running_requests is None:
        raise ValueError(
            "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD requires an explicit "
            "--max-running-requests to bound the per-process host KV allocation."
        )
    return True


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
    server_args: ServerArgs,
    use_mla_backend: bool,
    num_layers: int,
    element_size: int,
) -> Optional[int]:
    if not is_sparsity_driven_kv_offload_enabled(
        model_config=model_config,
        server_args=server_args,
        use_mla_backend=use_mla_backend,
    ):
        return None

    index_head_dim = get_sparsity_driven_kv_offload_index_head_dim(
        model_config=model_config
    )
    return index_head_dim * num_layers * element_size


def get_sparsity_driven_kv_offload_fixed_memory_size(
    *,
    model_config: ModelConfig,
    server_args: ServerArgs,
    use_mla_backend: bool,
    num_layers: int,
    element_size: int,
    max_running_requests_per_worker: int,
) -> Optional[int]:
    """Return the fixed device-KV allocation made by the sparse KV manager.

    In addition to the token-scaled index pool, ``SparseKVCacheManager`` keeps
    two sparse-context windows of full MLA KV for every request and layer. The
    request-to-token pool has one extra padding row, which the manager also
    allocates, so it must be included in the memory budget.
    """
    if not is_sparsity_driven_kv_offload_enabled(
        model_config=model_config,
        server_args=server_args,
        use_mla_backend=use_mla_backend,
    ):
        return None

    max_running_requests_per_worker = int(max_running_requests_per_worker)
    if max_running_requests_per_worker <= 0:
        raise ValueError(
            "Sparsity-driven KV offload requires a positive per-worker "
            "max_running_requests, got "
            f"{max_running_requests_per_worker}."
        )

    sparse_context_len = get_sparsity_driven_kv_offload_sparse_context_len(
        model_config=model_config
    )
    device_cache_capacity = (
        SPARSE_KV_DEVICE_CACHE_WINDOW_COUNT * sparse_context_len
    )
    kv_head_dim = int(model_config.kv_lora_rank) + int(
        model_config.qk_rope_head_dim
    )
    request_capacity = max_running_requests_per_worker + 1
    return (
        request_capacity
        * device_cache_capacity
        * kv_head_dim
        * num_layers
        * element_size
    )
