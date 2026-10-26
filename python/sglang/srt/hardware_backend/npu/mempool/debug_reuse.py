"""Temporary, opt-in ticket03 probe; remove after the NPU mismatch is resolved.

This observes acceptance data only. It neither changes model inputs/outputs nor
turns numerical differences into an acceptance tolerance.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Sequence

import torch
from torch import Tensor

DEBUG_ENV = "SGLANG_NPU_MEMPOOL_DEBUG_REUSE"
DEBUG_TAG = "[DEBUG-ticket03-reuse]"
logger = logging.getLogger(__name__)


class ReuseProbe:
    """Retain one cold prompt's last page, then compare identical prompts only."""

    def __init__(self, *, page_size: int, rank: int) -> None:
        self.page_size, self.rank = page_size, rank
        self.rid = ""
        self.input_ids: tuple[int, ...] = ()
        self.hit_len = 0
        self.reference_rid = ""
        self.reference_ids: tuple[int, ...] = ()
        self.reference: dict[tuple[str, int], Tensor] = {}
        self.indexer_layer_ids: tuple[int, ...] = ()
        self.start_layer = 0
        self.forward_seen = False
        self.finished = False

    def begin_request(self, rid: str, input_ids: Sequence[int], hit_len: int) -> None:
        if not input_ids or len(input_ids) % self.page_size:
            raise ValueError("reuse probe requires a full-page acceptance prompt")
        self.rid, self.input_ids, self.hit_len = rid, tuple(input_ids), hit_len
        self.forward_seen = self.finished = False

    def _report(self, stage: str, **data: Any) -> None:
        logger.info(
            "%s data=%s",
            DEBUG_TAG,
            json.dumps(dict(stage=stage, rank=self.rank, rid=self.rid, **data)),
        )

    def forward(
        self,
        batch: Any,
        *,
        block_table: Tensor,
        query_lengths: Tensor,
        kv_lengths: Tensor,
        restored_indices: Tensor | None,
        indexer_layer_ids: Sequence[int],
        start_layer: int,
    ) -> None:
        if not self.rid or self.forward_seen:
            return
        self.forward_seen = True
        self.indexer_layer_ids = tuple(indexer_layer_ids)
        self.start_layer = start_layer
        tokens, hit = len(self.input_ids), self.hit_len
        positions = batch.positions.detach().cpu().reshape(-1).tolist()
        inputs = batch.input_ids.detach().cpu().reshape(-1).tolist()
        suffix = batch.out_cache_loc.detach().cpu().long().reshape(-1)
        restored = (
            torch.empty(0, dtype=torch.int64)
            if restored_indices is None
            else restored_indices.detach().cpu().long().reshape(-1)
        )
        actual_blocks = block_table.detach().cpu().long()
        expected_map = torch.cat((restored, suffix))
        offsets = torch.arange(self.page_size, dtype=torch.int64)
        block_map = (actual_blocks[..., None] * self.page_size + offsets).reshape(-1)
        q_lengths = query_lengths.detach().cpu().reshape(-1).tolist()
        k_lengths = kv_lengths.detach().cpu().reshape(-1).tolist()
        prefix_lengths = batch.extend_prefix_lens.detach().cpu().tolist()
        extend_lengths = batch.extend_seq_lens.detach().cpu().tolist()
        checks = dict(
            input_ids=inputs == list(self.input_ids[hit:]),
            positions=positions == list(range(hit, tokens)),
            query_lengths=q_lengths == [tokens - hit],
            kv_lengths=k_lengths == [tokens],
            prefix_lengths=prefix_lengths == [hit],
            extend_lengths=extend_lengths == [tokens - hit],
            restored_length=restored.numel() == hit,
            suffix_length=suffix.numel() == tokens - hit,
            block_table=actual_blocks.shape[0] == 1
            and torch.equal(block_map, expected_map),
            unique_indices=expected_map.numel() == tokens
            and expected_map.unique().numel() == tokens
            and bool(torch.all(expected_map > 0)),
        )
        self._report(
            "forward",
            hit_len=hit,
            prompt_tokens=tokens,
            input_sha256=hashlib.sha256(
                json.dumps(self.input_ids).encode()
            ).hexdigest(),
            query_lengths=q_lengths,
            kv_lengths=k_lengths,
            position_endpoints=positions[:1] + positions[-1:],
            block_table=actual_blocks.tolist(),
            checks=checks,
        )

    def finish(self, values: dict[tuple[str, int], Tensor]) -> None:
        """Read completed CPU references already validated against alive storage."""
        if self.finished:
            return
        self.finished = True
        start = len(self.input_ids) - self.page_size
        if not self.reference and self.hit_len == 0:
            self.reference = {
                key: value[start:].clone() for key, value in values.items()
            }
            self.reference_rid, self.reference_ids = self.rid, self.input_ids
            self._report("baseline", page=start // self.page_size, layers=len(values))
            return
        if self.input_ids != self.reference_ids:
            self._report("different_input", reference_rid=self.reference_rid)
            return
        if set(values) != set(self.reference):
            raise RuntimeError("reuse probe layer coverage differs from cold baseline")
        for (region, layer), reference in sorted(self.reference.items()):
            current = values[region, layer][start:]
            left, right = reference.float(), current.float()
            finite = torch.isfinite(left) & torch.isfinite(right)
            changed = reference != current
            delta = (left - right)[finite].abs()
            first = changed.nonzero()[:1].tolist()
            if first:
                first[0][0] += start
            self._report(
                "layer",
                reference_rid=self.reference_rid,
                region=region,
                layer=layer,
                model_layer=(
                    self.indexer_layer_ids[layer]
                    if region == "index" and self.indexer_layer_ids
                    else self.start_layer + layer
                    if region == "main"
                    else None
                ),
                elements=reference.numel(),
                changed=int(changed.sum()),
                nonfinite=int((~finite).sum()),
                max_abs=float(delta.max()) if delta.numel() else None,
                rms=float(delta.square().mean().sqrt()) if delta.numel() else None,
                reference_max_abs=float(left[finite].abs().max())
                if delta.numel()
                else None,
                first_difference=first[0] if first else None,
            )
        self._report(
            "comparison",
            reference_rid=self.reference_rid,
            hit_len=self.hit_len,
            page=start // self.page_size,
            main_layers=sum(region == "main" for region, _ in values),
            index_layers=sum(region == "index" for region, _ in values),
        )
