"""Independent forward examples and DRAM references for the runtime writer gate."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple

import torch

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.runtime import KVWriteExpectation
else:
    from .runtime import KVWriteExpectation

WRITER_SENTINEL = -37


class WriterBinding(NamedTuple):
    """Install one request row in a physical slot with its full prompt length."""

    req_pool_idx: int
    slot: int
    prompt_tokens: int


class WriterDestination(NamedTuple):
    """Locate an expected source row in the owner's independent DRAM reference."""

    source_row: int
    slot: int
    position: int


@dataclass(frozen=True)
class WriterCase:
    """Describe forward fields plus explicit source-row to DRAM coordinates."""

    name: str
    decode: bool
    req_ids: tuple[int, ...]
    lengths: tuple[int, ...]
    prefixes: tuple[int, ...]
    source_rows: int
    bindings: tuple[WriterBinding, ...]
    writes: tuple[KVWriteExpectation, ...]
    destinations: tuple[WriterDestination, ...]
    seed: int
    tail_rows: bool = False
    reset_bindings: bool = False

    def batch(self, device: str) -> Any:
        """Allocate ordinary synthetic forward fields, not a SGLang service batch."""
        return SimpleNamespace(
            forward_mode=SimpleNamespace(is_decode=lambda: self.decode),
            req_pool_indices=torch.tensor(self.req_ids, device=device),
            seq_lens=torch.tensor(self.lengths, device=device) if self.decode else None,
            extend_seq_lens=None
            if self.decode
            else torch.tensor(self.lengths, device=device),
            extend_prefix_lens=None
            if self.decode
            else torch.tensor(self.prefixes, device=device),
            extend_seq_lens_cpu=None if self.decode else list(self.lengths),
            global_num_token_non_padded_cpu=sum(self.lengths)
            if self.tail_rows
            else None,
            out_cache_loc=torch.arange(self.source_rows, device=device),
        )

    def values(self, layer: int, dim: int) -> torch.Tensor:
        """Use exactly representable BF16 payloads that change by case/layer/row."""
        rows = torch.arange(self.source_rows).unsqueeze(1)
        columns = torch.arange(dim).unsqueeze(0)
        return (
            ((self.seed + layer * 43 + rows * 7 + columns) % 251)
            .to(torch.bfloat16)
            .unsqueeze(1)
        )

    def update_reference(self, reference: torch.Tensor, layer: int) -> None:
        """Apply explicit worked coordinates, independently of runtime row inference."""
        values = self.values(layer, reference.shape[-1])
        for source_row, slot, position in self.destinations:
            reference[slot, position] = values[source_row]


def prefill_cases(tokens: int) -> list[WriterCase]:
    """Cover chunk offsets, unbound/tail rows, slot reuse and the last P address."""
    if tokens < 4:
        raise ValueError("writer prefill cases require at least four tokens")
    return [
        WriterCase(
            name="ragged_tail_unbound",
            decode=False,
            req_ids=(1, 2, 3),
            lengths=(2, 1, 1),
            prefixes=(0, 0, 0),
            source_rows=6,
            bindings=(WriterBinding(1, 2, 4), WriterBinding(2, 7, 2)),
            writes=(KVWriteExpectation(1, 0, 2), KVWriteExpectation(2, 0, 1)),
            destinations=(
                WriterDestination(0, 2, 0),
                WriterDestination(1, 2, 1),
                WriterDestination(2, 7, 0),
            ),
            seed=11,
            tail_rows=True,
        ),
        WriterCase(
            name="chunk_prefix",
            decode=False,
            req_ids=(1, 2),
            lengths=(2, 1),
            prefixes=(2, 1),
            source_rows=3,
            bindings=(),
            writes=(KVWriteExpectation(1, 2, 2), KVWriteExpectation(2, 1, 1)),
            destinations=(
                WriterDestination(0, 2, 2),
                WriterDestination(1, 2, 3),
                WriterDestination(2, 7, 1),
            ),
            seed=67,
        ),
        WriterCase(
            name="same_slot_overwrite",
            decode=False,
            req_ids=(1,),
            lengths=(2,),
            prefixes=(0,),
            source_rows=2,
            bindings=(WriterBinding(1, 2, 2),),
            writes=(KVWriteExpectation(1, 0, 2),),
            destinations=(WriterDestination(0, 2, 0), WriterDestination(1, 2, 1)),
            seed=103,
            reset_bindings=True,
        ),
        WriterCase(
            name="last_slot_full_prompt",
            decode=False,
            req_ids=(1,),
            lengths=(tokens,),
            prefixes=(0,),
            source_rows=tokens,
            bindings=(WriterBinding(1, 15, tokens),),
            writes=(KVWriteExpectation(1, 0, tokens),),
            destinations=tuple(
                WriterDestination(row, 15, row) for row in range(tokens)
            ),
            seed=139,
            reset_bindings=True,
        ),
    ]


def decode_cases(tokens: int) -> list[WriterCase]:
    """Use a fixed 16-row decode graph with changed slots, prompts and positions."""
    if tokens < 2:
        raise ValueError("writer decode cases require at least two tokens")
    padding = (0,) * 14
    ones = (1,) * 14
    return [
        WriterCase(
            name="all_invalid",
            decode=True,
            req_ids=(0,) * 16,
            lengths=(1,) * 16,
            prefixes=(),
            source_rows=16,
            bindings=(),
            writes=(),
            destinations=(),
            seed=3,
        ),
        WriterCase(
            name="decode_first",
            decode=True,
            req_ids=(1, 2) + padding,
            lengths=(4, 6) + ones,
            prefixes=(),
            source_rows=16,
            bindings=(WriterBinding(1, 4, 3), WriterBinding(2, 9, 5)),
            writes=(KVWriteExpectation(1, 3, 1), KVWriteExpectation(2, 5, 1)),
            destinations=(WriterDestination(0, 4, 0), WriterDestination(1, 9, 0)),
            seed=29,
        ),
        WriterCase(
            name="decode_next",
            decode=True,
            req_ids=(1, 2) + padding,
            lengths=(5, 7) + ones,
            prefixes=(),
            source_rows=16,
            bindings=(),
            writes=(KVWriteExpectation(1, 4, 1), KVWriteExpectation(2, 6, 1)),
            destinations=(WriterDestination(0, 4, 1), WriterDestination(1, 9, 1)),
            seed=71,
        ),
        WriterCase(
            name="decode_rebind",
            decode=True,
            req_ids=(1, 2) + padding,
            lengths=(7, 3) + ones,
            prefixes=(),
            source_rows=16,
            bindings=(WriterBinding(1, 4, 6), WriterBinding(2, 11, 2)),
            writes=(KVWriteExpectation(1, 6, 1), KVWriteExpectation(2, 2, 1)),
            destinations=(WriterDestination(0, 4, 0), WriterDestination(1, 11, 0)),
            seed=113,
            reset_bindings=True,
        ),
        # Prime the written prefix eagerly so the final graph write tests S_D-1.
        WriterCase(
            name="decode_bounds_prime",
            decode=False,
            req_ids=(1,),
            lengths=(tokens - 1,),
            prefixes=(3,),
            source_rows=tokens - 1,
            bindings=(WriterBinding(1, 15, 3),),
            writes=(KVWriteExpectation(1, 3, tokens - 1),),
            destinations=tuple(
                WriterDestination(row, 15, row) for row in range(tokens - 1)
            ),
            seed=151,
            reset_bindings=True,
        ),
        WriterCase(
            name="decode_last_row",
            decode=True,
            req_ids=(1,) + (0,) * 15,
            lengths=(3 + tokens,) + (1,) * 15,
            prefixes=(),
            source_rows=16,
            bindings=(),
            writes=(KVWriteExpectation(1, 3 + tokens - 1, 1),),
            destinations=(WriterDestination(0, 15, tokens - 1),),
            seed=193,
        ),
    ]
