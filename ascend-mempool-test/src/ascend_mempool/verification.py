"""Known BF16 payloads and independent host references for the experiment."""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.copy import SparseCopyInputs
    from sglang.srt.hardware_backend.npu.mempool.layout import PoolLayout
    from torch import Tensor

else:
    from .layout import PoolLayout

SENTINEL = -7


def kv_pattern(
    rank: int,
    layer: int,
    slot: int,
    first_token: int,
    count: int,
    heads: int,
    dim: int,
) -> Tensor:
    import torch

    tokens = torch.arange(first_token, first_token + count).reshape(-1, 1, 1)
    columns = torch.arange(dim).reshape(1, 1, -1)
    head_ids = torch.arange(heads).reshape(1, -1, 1)
    features = (
        torch.full_like(tokens, rank + 1),
        torch.full_like(tokens, layer % 256),
        torch.full_like(tokens, slot % 256),
        tokens.remainder(256),
        torch.div(tokens, 256, rounding_mode="floor").remainder(256),
        torch.div(tokens, 65536, rounding_mode="floor").remainder(256),
        columns.remainder(256),
        (head_ids * 17 + layer * 11 + slot * 7 + columns // 256).remainder(256),
    )
    values = torch.zeros((count, heads, dim), dtype=torch.float32)
    for channel, feature in enumerate(features):
        values = torch.where(columns.remainder(8) == channel, feature.float(), values)
    return values.to(torch.bfloat16)


class CopyCase(msgspec.Struct):
    name: str
    p_slots: list[int]
    d_slots: list[int]
    prompt_lengths: list[int]
    decode_lengths: list[int]
    positions: list[list[int]]
    valid: list[list[bool]]
    active: list[bool]

    def load(self, inputs: SparseCopyInputs) -> None:
        import torch

        for name in (
            "p_slots",
            "d_slots",
            "prompt_lengths",
            "decode_lengths",
            "positions",
            "valid",
            "active",
        ):
            target = getattr(inputs, name)
            source = torch.tensor(getattr(self, name), dtype=target.dtype)
            if source.shape != target.shape:
                raise ValueError(f"case {name} shape differs from fixed graph input")
            target.copy_(source)


def make_cases(
    layout: PoolLayout, batch_rows: int, topk: int, active_rows: int, cycle: int = 0
) -> list[CopyCase]:
    if not 1 <= active_rows <= min(batch_rows, layout.prompt.slots):
        raise ValueError("active_rows must fit the physical slots and graph rows")
    names = (
        "prompt_only",
        "decode_only",
        "mixed",
        "masked",
        "last_slots_bounds",
        "short_written",
        "one_real_padded",
        "zero_valid",
        "empty_batch",
        "zero_prompt",
    )
    cases = []
    for variant, name in enumerate(names):
        case = CopyCase(name, [], [], [], [], [], [], [])
        real_rows = 1 if name == "one_real_padded" else active_rows
        for row in range(batch_rows):
            p_slot = (2 + row + variant + cycle * 3) % layout.prompt.slots
            d_slot = (5 + row + variant + cycle * 5) % layout.decode.slots
            p_len = layout.prompt.tokens
            d_len = layout.decode.tokens
            if name in ("mixed", "masked", "short_written", "one_real_padded"):
                p_len = min(p_len, 7 + row + cycle)
                d_len = min(d_len, 5 + row + cycle)
            if name == "zero_prompt":
                p_len = 0
                d_len = min(d_len, 5 + row)
            if name == "last_slots_bounds":
                p_slot = layout.prompt.slots - 1 - row % layout.prompt.slots
                d_slot = layout.decode.slots - 1 - (row + 1) % layout.decode.slots
            if name == "prompt_only":
                positions = [0, p_len - 1, p_len // 2, -1]
            elif name in ("decode_only", "zero_prompt"):
                positions = [p_len, p_len + d_len - 1, p_len + d_len // 2, -1]
            else:
                positions = [
                    0,
                    p_len - 1,
                    p_len,
                    p_len + d_len - 1,
                    p_len + d_len,
                    -1,
                    p_len // 2,
                    p_len + d_len // 2,
                ]
            selected = [
                positions[(column + cycle) % len(positions)] for column in range(topk)
            ]
            active = row < real_rows and name != "empty_batch"
            if not active:
                p_slot = d_slot = 1 << 40
                selected = [1 << 40] * topk
            case.p_slots.append(p_slot)
            case.d_slots.append(d_slot)
            case.prompt_lengths.append(p_len)
            case.decode_lengths.append(d_len)
            case.positions.append(selected)
            case.valid.append(
                [
                    name != "zero_valid" and (name != "masked" or column % 3 != 1)
                    for column in range(topk)
                ]
            )
            case.active.append(active)
        cases.append(case)
    return cases


def expected_output(case: CopyCase, layout: PoolLayout, layer: int) -> Tensor:
    import torch

    expected = torch.full(
        (
            len(case.active),
            len(case.positions[0]),
            layout.prompt.heads,
            layout.prompt.dim,
        ),
        SENTINEL,
        dtype=torch.bfloat16,
    )
    for row, active in enumerate(case.active):
        if not active:
            continue
        p_slot, d_slot = case.p_slots[row], case.d_slots[row]
        p_len, d_len = case.prompt_lengths[row], case.decode_lengths[row]
        if not (
            0 <= p_slot < layout.prompt.slots
            and 0 <= d_slot < layout.decode.slots
            and 0 <= p_len <= layout.prompt.tokens
            and 0 <= d_len <= layout.decode.tokens
        ):
            continue
        for column, position in enumerate(case.positions[row]):
            if not case.valid[row][column] or not 0 <= position < p_len + d_len:
                continue
            rank = 0 if position < p_len else 1
            slot = p_slot if rank == 0 else d_slot
            token = position if rank == 0 else position - p_len
            expected[row, column] = kv_pattern(
                rank, layer, slot, token, 1, layout.prompt.heads, layout.prompt.dim
            )[0]
    return expected
