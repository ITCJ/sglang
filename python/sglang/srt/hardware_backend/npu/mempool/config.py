"""Pure storage settings; runtime enablement and BM initialization are external."""

from dataclasses import dataclass, replace

from .layout import KVLayout, PoolLayout, positive_int

MEMPOOL_SLOTS = 16


@dataclass(frozen=True)
class MempoolConfig:
    """Set per-request P/D capacities for the fixed 16-slot MLA demo."""

    prefill_capacity: int = 16384
    decode_capacity: int = 16384

    def __post_init__(self) -> None:
        """Reject unusable token capacities before allocating device or BM memory."""
        positive_int("prefill_capacity", self.prefill_capacity)
        positive_int("decode_capacity", self.decode_capacity)

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
        prompt = KVLayout(
            layers=num_layers,
            slots=MEMPOOL_SLOTS,
            tokens=self.prefill_capacity,
            heads=1,
            dim=kv_lora_rank + qk_rope_head_dim,
            dtype=dtype,
        )
        return PoolLayout(prompt, replace(prompt, tokens=self.decode_capacity))
