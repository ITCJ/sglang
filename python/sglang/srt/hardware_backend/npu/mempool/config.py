"""Validate the fixed Ascend mempool topology before BM or graph initialization."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

import msgspec

from .layout import KVLayout, PoolLayout, positive_int

MEMPOOL_SLOTS = 16


class MempoolConfig(msgspec.Struct, frozen=True):
    """Set per-request P/D capacities for the fixed 16-slot MLA demo."""

    prefill_capacity: int = 16384
    decode_capacity: int = 16384
    prefill_host: str = ""
    nic: str = ""
    base_port: int = 19000
    bootstrap_port: int = 8998
    pool_id: int = 104
    timeout: float = 120.0

    def __post_init__(self) -> None:
        """Reject unusable token capacities before allocating device or BM memory."""
        positive_int("prefill_capacity", self.prefill_capacity)
        positive_int("decode_capacity", self.decode_capacity)

    @classmethod
    def from_server_args(
        cls, args: Any, *, sparse_enabled: bool, mla: bool, dtype: str, mlapo: bool
    ) -> MempoolConfig:
        """Check supported serving combinations and the paired pool connection."""
        if not sparse_enabled:
            raise ValueError("mempool requires sparse KV offload")
        if mlapo:
            raise ValueError("mempool does not support MLAPO")
        required = {
            "device": "npu",
            "tp_size": 16,
            "dp_size": 1,
            "pp_size": 1,
            "attn_cp_size": 1,
            "disaggregation_transfer_backend": "ascend",
            "disable_radix_cache": True,
        }
        for name, expected in required.items():
            if getattr(args, name, expected) != expected:
                raise ValueError(f"mempool requires {name}={expected!r}")
        if (
            args.disaggregation_mode not in ("prefill", "decode")
            or not mla
            or dtype != "bfloat16"
        ):
            raise ValueError("mempool requires BF16 MLA in Ascend PD mode")
        for name in (
            "speculative_algorithm",
            "enable_hierarchical_cache",
            "enable_hisparse",
            "disaggregation_decode_enable_radix_cache",
            "disaggregation_decode_enable_offload_kvcache",
            "enable_pd_role_switch",
            "enable_pdmux",
            "enable_prefill_cp",
            "enable_two_batch_overlap",
            "enable_lora",
            "enable_dynamic_chunking",
            "enable_mixed_chunk",
            "enable_unified_memory",
        ):
            if getattr(args, name, False):
                raise ValueError(f"mempool demo does not support {name}")
        if args.disaggregation_mode == "prefill" and not args.disable_cuda_graph:
            raise ValueError("mempool demo requires eager P (--disable-cuda-graph)")
        if getattr(args, "optimistic_prefill_attempts", 0):
            raise ValueError("mempool requires optimistic_prefill_attempts=0")
        config = cls(
            prefill_capacity=getattr(args, "mempool_prefill_capacity", 16384),
            decode_capacity=getattr(args, "mempool_decode_capacity", 16384),
            prefill_host=args.mempool_prefill_host or "",
            nic=args.mempool_nic or "",
            base_port=getattr(args, "mempool_base_port", 19000),
            bootstrap_port=getattr(args, "mempool_bootstrap_port", 8998),
            pool_id=getattr(args, "mempool_pool_id", 104),
            timeout=getattr(args, "mempool_timeout", 120.0),
        )
        if not config.prefill_host.strip() or not config.nic.startswith("tcp://"):
            raise ValueError(
                "mempool requires --mempool-prefill-host and --mempool-nic tcp://IP:PORT"
            )
        config.nic_for_rank(15)
        if (
            not 1 <= config.base_port <= 65520
            or not 1 <= config.bootstrap_port <= 65535
        ):
            raise ValueError("mempool store/PD bootstrap ports are out of range")
        if not 0 <= config.pool_id < 256:
            raise ValueError("mempool pool ID must stay below TransferEngine IDs (256)")
        if not math.isfinite(config.timeout) or config.timeout <= 0:
            raise ValueError("mempool timeout must be finite and positive")
        return config

    def nic_for_rank(self, tp_rank: int) -> str:
        """Reserve two NIC ports per pair; MF adds the local BM rank (0 or 1)."""
        address = urlsplit(self.nic)
        if (
            address.scheme != "tcp"
            or not address.hostname
            or address.port is None
            or address.path
            or address.query
            or address.fragment
            or address.username
            or not 0 <= tp_rank < 16
            or not 1 <= address.port <= 65504
        ):
            raise ValueError(
                "mempool NIC must be tcp://IP:PORT with 32 available ports"
            )
        host = address.hostname
        if ":" in host:
            host = f"[{host}]"
        return f"tcp://{host}:{address.port + 2 * tp_rank}"

    def make_mla_layout(
        self,
        *,
        num_layers: int,
        kv_lora_rank: int,
        qk_rope_head_dim: int,
        dtype: str = "bfloat16",
    ) -> PoolLayout:
        """Derive layer slabs from actual local layers and latent-plus-RoPE KV."""
        positive_int("kv_lora_rank", kv_lora_rank)
        positive_int("qk_rope_head_dim", qk_rope_head_dim)
        # Construct both sides so each capacity runs the layout bounds checks.
        prompt, decode = (
            KVLayout(
                layers=num_layers,
                slots=MEMPOOL_SLOTS,
                tokens=capacity,
                heads=1,
                dim=kv_lora_rank + qk_rope_head_dim,
                dtype=dtype,
            )
            for capacity in (self.prefill_capacity, self.decode_capacity)
        )
        return PoolLayout(prompt=prompt, decode=decode)
