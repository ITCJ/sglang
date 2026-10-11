"""Configuration and validation for sparsity-driven KV offload."""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:
    from sglang.srt.configs.model_config import ModelConfig
    from sglang.srt.mem_cache.kv_cache_configurator import KVCacheConfigurator

logger = logging.getLogger(__name__)


class SparseKVOffloadMode(str, Enum):
    DISABLED = "disabled"
    LOCAL_OFFLOAD = "local_offload"
    PD_PREFILL_NATIVE = "pd_prefill_native"
    PD_DECODE_OFFLOAD = "pd_decode_offload"
    PD_PREFILL_MEMPOOL = "pd_prefill_mempool"
    PD_DECODE_MEMPOOL = "pd_decode_mempool"

    @classmethod
    def from_flags(
        cls,
        *,
        sparse_enabled: bool,
        mempool_enabled: bool,
        disaggregation_mode: str,
        transfer_backend: str,
        max_running_requests: Optional[int],
    ) -> SparseKVOffloadMode:
        """Select storage capabilities without allocating or importing device code."""
        if not sparse_enabled:
            if mempool_enabled:
                raise ValueError("mempool requires sparse KV offload")
            return cls.DISABLED
        if max_running_requests is None or max_running_requests <= 0:
            raise ValueError(
                "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD requires positive "
                "max_running_requests to bound sparse HBM cache and request rows."
            )
        if disaggregation_mode == "null" and not mempool_enabled:
            return cls.LOCAL_OFFLOAD
        if disaggregation_mode not in ("prefill", "decode"):
            raise ValueError(
                "Sparse KV offload requires a supported PD role when mempool "
                f"is enabled; got disaggregation_mode={disaggregation_mode!r}."
            )
        if transfer_backend != "ascend":
            raise ValueError(
                "SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD with PD disaggregation "
                "requires disaggregation_transfer_backend='ascend'; got "
                f"{transfer_backend!r}."
            )
        if disaggregation_mode == "prefill":
            return cls.PD_PREFILL_MEMPOOL if mempool_enabled else cls.PD_PREFILL_NATIVE
        if mempool_enabled:
            return cls.PD_DECODE_MEMPOOL
        return cls.PD_DECODE_OFFLOAD

    @property
    def uses_sparse_kv_cache(self) -> bool:
        """Use an HBM sparse cache instead of full native compact KV storage."""
        return self in (
            SparseKVOffloadMode.LOCAL_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL,
        )

    @property
    def uses_host_kv_offload(self) -> bool:
        return self in (
            SparseKVOffloadMode.LOCAL_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
        )

    @property
    def uses_pd_decode_staging(self) -> bool:
        return self is SparseKVOffloadMode.PD_DECODE_OFFLOAD

    @property
    def uses_mempool_bm(self) -> bool:
        return self in (
            SparseKVOffloadMode.PD_PREFILL_MEMPOOL,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL,
        )

    @property
    def uses_index_k_only_transfer(self) -> bool:
        """Publish native Index K while compact KV stays in P/D BM."""
        return self in (
            SparseKVOffloadMode.PD_PREFILL_MEMPOOL,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL,
        )

    @property
    def uses_sparse_pd_transfer(self) -> bool:
        """Ascend transfer with independent main-KV and Index K layer lists."""
        return self in (
            SparseKVOffloadMode.PD_PREFILL_NATIVE,
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
            SparseKVOffloadMode.PD_PREFILL_MEMPOOL,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL,
        )


def resolve_sparse_kv_offload_mode(
    *,
    model_config: Optional[ModelConfig] = None,
    use_mla_backend: Optional[bool] = None,
) -> SparseKVOffloadMode:
    from sglang.srt.environ import envs

    sparse_enabled = envs.SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD.get()
    mempool_enabled = envs.SGLANG_NPU_ENABLE_MEMPOOL.get()
    if not sparse_enabled and not mempool_enabled:
        return SparseKVOffloadMode.DISABLED
    from sglang.srt.configs.model_config import is_deepseek_dsa
    from sglang.srt.runtime_context import (
        attention_backends,
        get_disagg,
        get_schedule,
        process_model_config,
        uses_mla_backend,
    )
    from sglang.srt.utils.common import is_npu

    disagg = get_disagg()
    mode = SparseKVOffloadMode.from_flags(
        sparse_enabled=sparse_enabled,
        mempool_enabled=mempool_enabled,
        disaggregation_mode=disagg.disaggregation_mode,
        transfer_backend=disagg.disaggregation_transfer_backend,
        max_running_requests=get_schedule().max_running_requests,
    )

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
    return mode


def configure_for_model_runner(model_runner: Any) -> None:
    """Validate startup choices before KV sizing/allocation, without opening BM."""
    mode = resolve_sparse_kv_offload_mode(
        model_config=model_runner.model_config,
        use_mla_backend=model_runner.use_mla_backend,
    )
    config = None
    if mode.uses_mempool_bm:
        from sglang.srt.hardware_backend.npu.attention.mla_preprocess import (
            is_mla_preprocess_enabled,
        )
        from sglang.srt.hardware_backend.npu.mempool.config import MempoolConfig

        config = MempoolConfig.from_server_args(
            model_runner.server_args,
            sparse_enabled=mode is not SparseKVOffloadMode.DISABLED,
            mla=model_runner.use_mla_backend,
            dtype=str(model_runner.kv_cache_dtype).removeprefix("torch."),
            mlapo=is_mla_preprocess_enabled(),
        )
        # Kernel/layout constraints are knowable before native or host KV exists.
        config.make_mla_layout(
            num_layers=model_runner.layer_info.end_layer
            - model_runner.layer_info.start_layer,
            kv_lora_rank=model_runner.model_config.kv_lora_rank,
            qk_rope_head_dim=model_runner.model_config.qk_rope_head_dim,
        )
    model_runner.sparse_kv_offload_mode = mode
    model_runner.mempool_config = config
    if mode is not SparseKVOffloadMode.DISABLED:
        logger.info(
            "Sparse KV startup: mode=%s sparse_cache=%s host_kv=%s "
            "pd_staging=%s mempool_bm=%s",
            mode.value,
            mode.uses_sparse_kv_cache,
            mode.uses_host_kv_offload,
            mode.uses_pd_decode_staging,
            mode.uses_mempool_bm,
        )


def get_sparsity_driven_kv_offload_sparse_context_len(
    *,
    model_config: ModelConfig,
) -> int:
    """Return the per-request on-device sparse KV window size."""
    from sglang.srt.configs.model_config import get_dsa_index_topk

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
        from sglang.srt.configs.model_config import get_dsa_index_head_dim

        index_head_dim = get_dsa_index_head_dim(model_config.hf_config)
    index_head_dim = int(index_head_dim)
    if index_head_dim <= 0:
        raise ValueError(
            "Sparsity-driven KV offload requires a positive DSA index_head_dim, "
            f"got {index_head_dim}."
        )
    return index_head_dim


def get_sparse_pd_indexer_layer_ids(
    kvc: KVCacheConfigurator,
) -> Optional[tuple[int, ...]]:
    """Select compact Index K only for the supported Ascend sparse PD path.

    Both sizing and allocation use this policy. Other transports and cache
    managers retain their existing layout; compact MLA KV still has every layer.
    """
    if (
        kvc.sparse_kv_offload_mode is None
        or not kvc.sparse_kv_offload_mode.uses_sparse_pd_transfer
    ):
        return None
    import torch

    from sglang.srt.configs.model_config import dsa_layer_skips_topk
    from sglang.srt.runtime_context import get_memory, get_parallel

    memory, parallel = get_memory(), get_parallel()
    if (
        kvc.kv_cache_dtype != torch.bfloat16
        or kvc.is_draft_worker
        or not kvc.spec_algorithm.is_none()
        or memory.enable_hisparse
        or memory.enable_hierarchical_cache
        or memory.enable_unified_cache_external_linker
        or parallel.pp_size != 1
        or parallel.attn_cp_size != 1
        or parallel.attn_dcp_size != 1
    ):
        return None
    hf_config = kvc.model_config.hf_config
    indexer_types = getattr(hf_config, "indexer_types", None)
    if indexer_types is not None and (
        not isinstance(indexer_types, (list, tuple))
        or len(indexer_types) != hf_config.num_hidden_layers
        or any(kind not in ("full", "shared") for kind in indexer_types)
    ):
        raise ValueError(
            "Ascend sparse PD indexer_types must contain one full/shared entry "
            "per model layer"
        )
    layer_ids = tuple(
        layer_id
        for layer_id in range(kvc.layer_info.start_layer, kvc.layer_info.end_layer)
        if not dsa_layer_skips_topk(hf_config, layer_id)
    )
    if not layer_ids or layer_ids[0] != kvc.layer_info.start_layer:
        raise ValueError("Ascend sparse PD requires a full indexer producer first")
    return layer_ids


def get_sparsity_driven_kv_offload_cell_size(
    *,
    model_config: ModelConfig,
    use_mla_backend: bool,
    num_layers: int,
    element_size: int,
    mode: Optional[SparseKVOffloadMode] = None,
    indexer_layer_ids: Optional[Sequence[int]] = None,
) -> Optional[int]:
    if mode is None:
        mode = resolve_sparse_kv_offload_mode(
            model_config=model_config,
            use_mla_backend=use_mla_backend,
        )
    if not mode.uses_sparse_kv_cache:
        return None

    index_head_dim = get_sparsity_driven_kv_offload_index_head_dim(
        model_config=model_config
    )
    indexer_layers = num_layers if indexer_layer_ids is None else len(indexer_layer_ids)
    return index_head_dim * indexer_layers * element_size
