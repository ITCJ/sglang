"""CPU tensor-order oracle, independent of production address arithmetic."""

from functools import lru_cache

import torch


@lru_cache(maxsize=None)
def _row_table(slots, tokens, pages, layers, layer, chunks):
    # Each layer gets independently labelled logical rows. Tensor dimension
    # insertion supplies physical order, rather than calling the tested formula.
    logical = torch.arange(slots * tokens * chunks).reshape(
        slots, pages, tokens // pages, chunks
    )
    physical = torch.full(
        (slots, pages, layers, tokens // pages, chunks), -1, dtype=torch.int64
    )
    physical[:, :, layer] = logical
    return physical.flatten()[layer * (tokens // pages) * chunks :]


def logical_rows(view, indices, row_bytes):
    layout = view.layout
    table = _row_table(
        layout.slots,
        layout.tokens,
        layout.tokens // layout.page_size,
        layout.layers,
        view.layer,
        layout.row_bytes // row_bytes,
    )
    result = table[indices]
    if (result < 0).any():
        raise AssertionError("copy accessed a different layer in the page")
    return result


def read_rows(view, tensor, indices, row_bytes):
    return tensor.reshape(-1, row_bytes // 2)[logical_rows(view, indices, row_bytes)]


def write_rows(view, tensor, indices, row_bytes, values):
    tensor.reshape(-1, row_bytes // 2)[logical_rows(view, indices, row_bytes)] = values
