"""Fixed region tables and task buffers for the binary-v1 copy kernel."""

from __future__ import annotations

import importlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import torch
from torch import Tensor


def view_regions(views: Sequence[Any], row_bytes: int) -> list[tuple[int, int]]:
    return [
        (
            view.device_base,
            (
                view.layout.total_bytes
                - view.layer * view.layout.page_size * view.layout.row_bytes
            )
            // row_bytes,
        )
        if view is not None
        else (0, 1)
        for view in views
    ]


def load_kernel() -> Any:
    """Load the standalone extension without replacing the installed SGL library."""
    importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")

    if not hasattr(torch.ops.npu, "unidex_copy_multibase"):
        library = os.environ.get(
            "SGLANG_UNIDEX_MULTIBASE_LIBRARY",
            str(
                Path(__file__).parent
                / "kernels/unidex_multibase/build/libunidex_multibase.so"
            ),
        )
        torch.ops.load_library(library)
    return torch.ops.npu.unidex_copy_multibase


class BaseTable:
    """Ordered disjoint virtual row ranges; mapped owners outlive their uses."""

    def __init__(
        self,
        regions: Sequence[tuple[int, int]],
        device: Any,
        owners: Sequence[Any] = (),
    ) -> None:
        self.owners = tuple(owners)
        ranges = []
        cursor = 0
        for _, count in regions:
            ranges.append((cursor, cursor + count))
            cursor += count
        self.starts = tuple(begin for begin, _ in ranges)
        # Cold calls can occur inside Graph capture. Allocate on device and
        # initialize with captured fills, avoiding synchronous H2D tensor uploads.
        self.bases = torch.empty(len(regions), dtype=torch.int64, device=device)
        self.ranges = torch.empty((len(regions), 2), dtype=torch.int64, device=device)
        self.valid = torch.empty(len(regions), dtype=torch.bool, device=device)
        for i, ((pointer, count), (begin, end)) in enumerate(zip(regions, ranges)):
            self.bases[i].fill_(pointer)
            self.ranges[i, 0].fill_(begin)
            self.ranges[i, 1].fill_(end)
            self.valid[i].fill_(pointer != 0 and count > 0)

    @property
    def metadata(self) -> tuple[Tensor, Tensor, Tensor]:
        return self.bases, self.ranges, self.valid


class MultibaseCopy:
    """One launch with fixed row indices and validity storage."""

    def __init__(
        self,
        source: BaseTable,
        target: BaseTable,
        tasks: int,
        *,
        row_bytes: int,
        block_dim: int = 48,
        kernel: Any = None,
    ) -> None:
        self.source, self.target = source, target
        self.row_bytes, self.block_dim = row_bytes, block_dim
        self.kernel = kernel
        self.source_rows = torch.zeros(
            tasks, dtype=torch.int64, device=source.bases.device
        )
        self.target_rows = torch.zeros_like(self.source_rows)
        self.valid = torch.zeros_like(self.source_rows, dtype=torch.bool)

    @property
    def metadata(self) -> tuple[Tensor, ...]:
        return (
            *self.source.metadata,
            *self.target.metadata,
            self.source_rows,
            self.target_rows,
            self.valid,
        )

    def submit(self, source_rows: Tensor, target_rows: Tensor, valid: Tensor) -> None:
        count = source_rows.numel()
        self.source_rows[:count].copy_(source_rows.flatten())
        self.target_rows[:count].copy_(target_rows.flatten())
        self.valid[:count].copy_(valid.flatten())
        self.valid[count:].zero_()
        self.launch()

    def launch(self) -> None:
        if self.kernel is None:
            self.kernel = load_kernel()
        self.kernel(*self.metadata, self.row_bytes, self.block_dim)
