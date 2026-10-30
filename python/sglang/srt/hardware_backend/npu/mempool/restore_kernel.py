"""Scatter compact gathered rows into native MLA or Index K in one NPU launch."""

import triton
import triton.language as tl


@triton.jit
def _scatter_rows(
    source,
    table,
    key,
    rope,
    ROWS: tl.constexpr,
    KEY_DIM: tl.constexpr,
    ROPE_DIM: tl.constexpr,
    PAGE_SIZE: tl.constexpr,
    NZ: tl.constexpr,
    BLOCK: tl.constexpr,
):
    row = tl.program_id(0)
    source_row = tl.load(table + row).to(tl.int64)
    target_row = tl.load(table + ROWS + row).to(tl.int64)
    column = tl.arange(0, BLOCK)
    values = tl.load(
        source + source_row * (KEY_DIM + ROPE_DIM) + column,
        column < KEY_DIM + ROPE_DIM,
        other=0,
    )
    if NZ:
        key_offset = (
            target_row // PAGE_SIZE * PAGE_SIZE * KEY_DIM
            + column // 16 * PAGE_SIZE * 16
            + target_row % PAGE_SIZE * 16
            + column % 16
        )
    else:
        key_offset = target_row * KEY_DIM + column
    tl.store(key + key_offset, values, column < KEY_DIM)
    if ROPE_DIM:
        rope_column = column - KEY_DIM
        if NZ:
            rope_offset = (
                target_row // PAGE_SIZE * PAGE_SIZE * ROPE_DIM
                + rope_column // 16 * PAGE_SIZE * 16
                + target_row % PAGE_SIZE * 16
                + rope_column % 16
            )
        else:
            rope_offset = target_row * ROPE_DIM + rope_column
        tl.store(
            rope + rope_offset,
            values,
            (column >= KEY_DIM) & (column < KEY_DIM + ROPE_DIM),
        )


def scatter_rows(source, table, key, rope, key_dim, rope_dim, page_size, nz):
    """table[0] holds valid slot rows, table[1] their allocator-owned HBM rows."""
    args = (source, table, key, rope if rope is not None else key)
    constants = dict(
        ROWS=table.shape[1],
        KEY_DIM=key_dim,
        ROPE_DIM=rope_dim,
        PAGE_SIZE=page_size,
        NZ=nz,
        BLOCK=triton.next_power_of_2(key_dim + rope_dim),
    )
    _scatter_rows[(table.shape[1],)](*args, **constants)
