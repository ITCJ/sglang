"""Temporary, opt-in ticket03 probe; remove after the NPU mismatch is resolved.

This observes acceptance data only. It neither changes model inputs/outputs nor
turns numerical differences into an acceptance tolerance.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Sequence

import torch
from torch import Tensor

DEBUG_ENV = "SGLANG_NPU_MEMPOOL_DEBUG_REUSE"
DEBUG_TAG = "[DEBUG-ticket03-reuse]"
CHECKPOINT_BUDGET_BYTES = 64 * 2**20
logger = logging.getLogger(__name__)


def _difference(reference: Tensor, current: Tensor, start: int) -> dict[str, Any]:
    if reference.shape != current.shape:
        raise ValueError("reuse diagnostic tensor shapes differ")
    left, right = reference.float(), current.float()
    finite = torch.isfinite(left) & torch.isfinite(right)
    changed = reference != current
    delta = (left - right)[finite].abs()
    first = changed.nonzero()[:1].tolist()
    if first:
        first[0][0] += start
    return dict(
        elements=reference.numel(),
        changed=int(changed.sum()),
        nonfinite=int((~finite).sum()),
        max_abs=float(delta.max()) if delta.numel() else None,
        rms=float(delta.square().mean().sqrt()) if delta.numel() else None,
        reference_max_abs=float(left[finite].abs().max()) if delta.numel() else None,
        first_difference=first[0] if first else None,
    )


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
        self.checkpoints: dict[str, Tensor] = {}
        self.checkpoints_seen: set[str] = set()
        self.attention_tail: Tensor | None = None

    def _check_retention(self, candidate: Tensor) -> None:
        retained = sum(t.numel() * t.element_size() for t in self.checkpoints.values())
        if self.attention_tail is not None:
            retained += self.attention_tail.numel() * self.attention_tail.element_size()
        if (
            retained + candidate.numel() * candidate.element_size()
            > CHECKPOINT_BUDGET_BYTES
        ):
            raise RuntimeError(
                "reuse checkpoints exceed the 64 MiB per-rank diagnostic budget"
            )

    def begin_request(self, rid: str, input_ids: Sequence[int], hit_len: int) -> None:
        if not input_ids or len(input_ids) % self.page_size:
            raise ValueError("reuse probe requires a full-page acceptance prompt")
        self.rid, self.input_ids, self.hit_len = rid, tuple(input_ids), hit_len
        self.forward_seen = self.finished = False
        self.checkpoints_seen.clear()
        if not self.reference_rid and hit_len == 0:
            self.reference_rid, self.reference_ids = rid, self.input_ids

    def observes_current(self) -> bool:
        return bool(self.rid) and self.input_ids == self.reference_ids

    def checkpoint(self, name: str, value: Tensor, *, sort_last: bool = False) -> None:
        if not self.observes_current() or name in self.checkpoints_seen:
            return
        self.checkpoints_seen.add(name)
        if value.shape[0] != len(self.input_ids) - self.hit_len:
            self._report("checkpoint_error", name=name, shape=list(value.shape))
            return
        # Slice before copying: neither the full prompt nor an NPU view is retained.
        tail = value[-self.page_size :].detach().cpu().clone()
        if sort_last:
            tail = tail.sort(dim=-1).values
        if self.rid == self.reference_rid:
            self._check_retention(tail)
            self.checkpoints[name] = tail
            self._report("checkpoint_baseline", name=name, shape=list(tail.shape))
        elif name not in self.checkpoints:
            self._report("checkpoint_error", name=name, error="missing cold baseline")
        else:
            self._report(
                "checkpoint",
                name=name,
                reference_rid=self.reference_rid,
                **_difference(
                    self.checkpoints[name], tail, len(self.input_ids) - self.page_size
                ),
            )

    def inspect_attention(
        self, kwargs: dict[str, Any], output: Tensor, call: Callable[..., Any]
    ) -> None:
        """Observe layer 0; extra calls never replace the model's original output."""
        if not self.observes_current() or "layer0.attention" in self.checkpoints_seen:
            return
        self.checkpoint("layer0.query", kwargs["query"])
        self.checkpoint("layer0.query_rope", kwargs["query_rope"])
        self.checkpoint("layer0.topk", kwargs["sparse_indices"])
        self.checkpoint("layer0.topk_set", kwargs["sparse_indices"], sort_last=True)
        self.checkpoint("layer0.attention", output)
        start = len(self.input_ids) - self.page_size
        actual = output[-self.page_size :].detach().cpu().clone()
        if self.rid == self.reference_rid:
            if (
                kwargs["layout_query"] != "TND"
                or kwargs["layout_kv"] != "PA_BSND"
                or kwargs["sparse_mode"] != 3
                or kwargs["attention_mode"] != 2
                or kwargs["actual_seq_lengths_query"].numel() != 1
                or kwargs["query"].shape[0] != len(self.input_ids)
            ):
                self._report(
                    "checkpoint_error",
                    name="attention_replay",
                    error="unsupported layout",
                )
                return
            again = call(**kwargs)[0][-self.page_size :].detach().cpu()
            self._report(
                "attention_replay",
                name="same_shape",
                reference_rid=self.reference_rid,
                **_difference(actual, again, start),
            )
            tail_kwargs = dict(kwargs)
            for key in ("query", "query_rope", "sparse_indices"):
                tail_kwargs[key] = kwargs[key][-self.page_size :].contiguous()
            tail_kwargs["actual_seq_lengths_query"] = torch.full_like(
                kwargs["actual_seq_lengths_query"], self.page_size
            )
            tail_result = call(**tail_kwargs)[0].detach().cpu().clone()
            self._check_retention(tail_result)
            self.attention_tail = tail_result
            self._report(
                "attention_replay",
                name="tail_query",
                reference_rid=self.reference_rid,
                **_difference(actual, self.attention_tail, start),
            )
        elif self.attention_tail is not None:
            self._report(
                "attention_replay",
                name="C_vs_A_tail",
                reference_rid=self.reference_rid,
                **_difference(self.attention_tail, actual, start),
            )

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
                **_difference(reference, current, start),
            )
        self._report(
            "comparison",
            reference_rid=self.reference_rid,
            hit_len=self.hit_len,
            page=start // self.page_size,
            main_layers=sum(region == "main" for region, _ in values),
            index_layers=sum(region == "index" for region, _ in values),
        )
