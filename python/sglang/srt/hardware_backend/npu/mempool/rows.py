"""Infer compact KV row coordinates without importing SGLang service types.

The layout/validity logic is copied from SparseKVCacheManager.offload_v2 in
../sparsity_driven_kv_offload/manager.py. Keep both copies aligned when fixing
padding or validity rules; see ticket02's S6 in
.scratch/ascend-mempool/issues/02-rank-pair-control-lifecycle.md.
"""

from __future__ import annotations

from typing import Optional, Sequence

import torch
from torch import Tensor


def derive_kv_rows(
    num_rows: int,
    *,
    is_decode: bool,
    max_context_len: int,
    req_pool_indices: Tensor,
    seq_lens: Optional[Tensor] = None,
    out_cache_loc: Optional[Tensor] = None,
    extend_seq_lens: Optional[Tensor] = None,
    extend_prefix_lens: Optional[Tensor] = None,
    extend_seq_lens_cpu: Optional[Sequence[int]] = None,
    global_num_token_non_padded_cpu: Optional[int] = None,
) -> tuple[Tensor, Tensor, Tensor]:
    """Return request rows, full-sequence token positions and native validity.

    Decode uses one row per request. Extend supports compact ragged rows,
    compact rows plus MoE tail padding, and static [batch, tokens_per_req].
    Host extend lengths are required to avoid a device read during capture.
    Binding and P/D capacity masks belong to the runtime, not this function.
    """
    if num_rows < 0 or max_context_len <= 0 or req_pool_indices.ndim != 1:
        raise ValueError("invalid KV row extent, context length or request shape")
    req_ids = req_pool_indices.to(torch.long)
    device = req_ids.device
    if is_decode:
        if seq_lens is None or out_cache_loc is None:
            raise ValueError("decode requires seq_lens and out_cache_loc")
        if req_ids.numel() != num_rows or seq_lens.shape != req_ids.shape:
            raise ValueError("decode compact KV rows must match batch size")
        if out_cache_loc.shape != req_ids.shape:
            raise ValueError("decode out_cache_loc rows must match compact KV")
        token_pos = (seq_lens - 1).to(torch.long)
        valid = (
            (seq_lens != 1)
            & (out_cache_loc >= 0)
            & (req_ids >= 0)
            & (token_pos >= 0)
            & (token_pos < max_context_len)
        )
        return req_ids, token_pos, valid.contiguous()
    if extend_seq_lens is None or extend_prefix_lens is None:
        raise ValueError("prefill requires extend_seq_lens and extend_prefix_lens")
    batch_size = req_ids.numel()
    if batch_size == 0:
        return (
            torch.full((num_rows,), -1, dtype=torch.long, device=device),
            torch.zeros(num_rows, dtype=torch.long, device=device),
            torch.zeros(num_rows, dtype=torch.bool, device=device),
        )
    lengths = extend_seq_lens.to(torch.long)
    prefixes = extend_prefix_lens.to(torch.long)
    if lengths.shape != req_ids.shape:
        raise ValueError("prefill extend_seq_lens must be padded to batch size")
    if prefixes.ndim != 1 or prefixes.numel() > batch_size:
        raise ValueError("prefill extend_prefix_lens length exceeds batch size")
    if prefixes.numel() < batch_size:
        prefixes = torch.cat(
            [
                prefixes,
                torch.zeros(
                    batch_size - prefixes.numel(), device=device, dtype=torch.long
                ),
            ]
        )
    length_sum = (
        sum(extend_seq_lens_cpu[:batch_size])
        if extend_seq_lens_cpu is not None
        else int(lengths.sum().item())
    )
    tail_padding = (
        global_num_token_non_padded_cpu == length_sum and length_sum < num_rows
    )
    if length_sum == num_rows or tail_padding:
        starts = torch.cumsum(lengths, dim=0) - lengths
        flat_req = torch.repeat_interleave(req_ids, lengths, output_size=length_sum)
        flat_starts = torch.repeat_interleave(starts, lengths, output_size=length_sum)
        flat_prefix = torch.repeat_interleave(prefixes, lengths, output_size=length_sum)
        pos = flat_prefix + torch.arange(length_sum, device=device) - flat_starts
        valid = (flat_req >= 0) & (pos >= 0) & (pos < max_context_len)
        if tail_padding:
            padding = num_rows - length_sum
            flat_req = torch.cat(
                [flat_req, torch.full((padding,), -1, device=device, dtype=torch.long)]
            )
            pos = torch.cat(
                [pos, torch.zeros(padding, device=device, dtype=torch.long)]
            )
            valid = torch.cat(
                [valid, torch.zeros(padding, device=device, dtype=torch.bool)]
            )
    else:
        if num_rows % batch_size:
            raise ValueError("prefill requires compact ragged or static row-major KV")
        columns = num_rows // batch_size
        offsets = (
            torch.arange(columns, device=device)
            .unsqueeze(0)
            .expand(batch_size, columns)
        )
        flat_req = req_ids.unsqueeze(1).expand(batch_size, columns).reshape(-1)
        pos = (prefixes.unsqueeze(1) + offsets).reshape(-1)
        valid = (
            (offsets < lengths.unsqueeze(1)).reshape(-1)
            & (flat_req >= 0)
            & (pos >= 0)
            & (pos < max_context_len)
        )
    if out_cache_loc is not None and out_cache_loc.numel() >= num_rows:
        valid = valid & (out_cache_loc[:num_rows] >= 0)
    return flat_req.contiguous(), pos.contiguous(), valid.contiguous()
