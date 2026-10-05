"""Configuration and validation for sparsity-driven KV offload."""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sglang.srt.configs.model_config import ModelConfig

logger = logging.getLogger(__name__)


class SparseKVOffloadMode(str, Enum):
    DISABLED = "disabled"
    LOCAL_OFFLOAD = "local_offload"
    PD_PREFILL_NATIVE = "pd_prefill_native"
    PD_DECODE_OFFLOAD = "pd_decode_offload"
    PD_PREFILL_MEMPOOL_SHADOW = "pd_prefill_mempool_shadow"
    PD_DECODE_MEMPOOL_SHADOW = "pd_decode_mempool_shadow"
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
            return (
                cls.PD_PREFILL_MEMPOOL_SHADOW
                if mempool_enabled
                else cls.PD_PREFILL_NATIVE
            )
        if mempool_enabled:
            # Keep service startup on shadow until the full ticket03 cutover.
            return cls.PD_DECODE_MEMPOOL_SHADOW
        return cls.PD_DECODE_OFFLOAD

    @property
    def uses_sparse_kv_cache(self) -> bool:
        """Use an HBM sparse cache instead of full native compact KV storage."""
        return self in (
            SparseKVOffloadMode.LOCAL_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL_SHADOW,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL,
        )

    @property
    def uses_host_kv_offload(self) -> bool:
        return self in (
            SparseKVOffloadMode.LOCAL_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL_SHADOW,
        )

    @property
    def uses_pd_decode_staging(self) -> bool:
        return self in (
            SparseKVOffloadMode.PD_DECODE_OFFLOAD,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL_SHADOW,
        )

    @property
    def uses_mempool_bm(self) -> bool:
        return self in (
            SparseKVOffloadMode.PD_PREFILL_MEMPOOL_SHADOW,
            SparseKVOffloadMode.PD_DECODE_MEMPOOL_SHADOW,
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

    def validate_runtime_support(self) -> None:
        """Do not launch before the remaining lifecycle/startup checks."""
        if self.uses_index_k_only_transfer:
            raise ValueError(
                "Formal mempool mode still requires ticket03 S5; "
                "only the existing shadow service path can be started."
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
    mode.validate_runtime_support()
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


def get_sparsity_driven_kv_offload_cell_size(
    *,
    model_config: ModelConfig,
    use_mla_backend: bool,
    num_layers: int,
    element_size: int,
    mode: Optional[SparseKVOffloadMode] = None,
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
    return index_head_dim * num_layers * element_size
