"""Fixed region tables and task buffers for the binary-v1 copy kernel."""

from __future__ import annotations

import importlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import torch
from torch import Tensor


def mapped_regions(manager: Any, layer: int, region: str = "main") -> list[Any]:
    """Capture the union needed by 25, including inactive prefix segments."""
    views = [manager.view(0, layer, region="alive_" + region)]
    if region == "main":
        views.append(manager.view(1, layer))
    prefix = "prefix_" + region
    session = manager.session
    count = (
        len(session.prefix_layout.groups) if session and session.prefix_layout else 1
    )
    for segment in range(max(8, count)):
        views.append(
            manager.view(
                0,
                layer,
                region=prefix,
                segment=segment if session and session.prefix_layout else None,
            )
            if prefix in manager.layout.regions(0) and segment < count
            else None
        )
    return views


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
        self.bases = torch.tensor(
            [p for p, _ in regions], dtype=torch.int64, device=device
        )
        self.ranges = torch.tensor(ranges, dtype=torch.int64, device=device).reshape(
            -1, 2
        )
        self.valid = torch.tensor(
            [p != 0 and n > 0 for p, n in regions], dtype=torch.bool, device=device
        )

    @property
    def metadata(self) -> tuple[Tensor, Tensor, Tensor]:
        return self.bases, self.ranges, self.valid


class MultibaseCopy:
    """One launch with fixed storage, including inactive future target tasks."""

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


class IndexedCopy:
    """Singleton adapter for existing logical-copy callers, using the same backend.

    Each caller owns this object. Warmup creates plans for its fixed shapes;
    pointer fills and task updates are then captured as ordinary device ops.
    No host tensor values are read. Graph-owned intermediates remain in the
    graph memory pool; mapped regions are retained by the caller's views.
    """

    def __init__(
        self,
        kernel: Any = None,
        target_capacity: int = 1,
        task_factor: int = 1,
        target_views: Sequence[Any] = (),
        target_slot: int = 0,
    ) -> None:
        self.kernel = kernel
        self.target_capacity, self.task_factor = target_capacity, task_factor
        self.target_views, self.target_slot = tuple(target_views), target_slot
        self.plans: dict[tuple[Any, ...], MultibaseCopy] = {}
        self.eager_key: tuple[Any, ...] | None = None
        self.eager_plan: MultibaseCopy | None = None

    def __call__(
        self,
        source: Tensor,
        target: Tensor,
        source_rows: Tensor,
        target_rows: Tensor,
        valid: Tensor,
        src_count: int,
        dst_count: int,
        row_bytes: int,
        count: int,
        block_dim: int,
        src_ptr: int | None = None,
        dst_ptr: int | None = None,
        *,
        target_view: Any = None,
    ) -> None:
        key = (
            str(source_rows.device),
            src_count,
            dst_count,
            row_bytes,
            count,
            block_dim,
        )
        capturing = (
            source_rows.device.type == "npu" and torch.npu.is_current_stream_capturing()
        )
        plan = self.plans.get(key) if capturing else None
        if plan is None and self.eager_key == key:
            plan = self.eager_plan
        if plan is None:
            src = BaseTable(
                [(src_ptr or source.data_ptr(), src_count)], source_rows.device
            )
            dst = BaseTable(
                view_regions(self.target_views, row_bytes)
                if self.target_views
                else [(dst_ptr or target.data_ptr(), dst_count)]
                + [(0, 1)] * (self.target_capacity - 1),
                source_rows.device,
                self.target_views,
            )
            plan = MultibaseCopy(
                src,
                dst,
                count * self.task_factor,
                row_bytes=row_bytes,
                block_dim=block_dim,
                kernel=self.kernel,
            )
        if capturing:
            self.plans[key] = plan
        else:
            self.eager_key, self.eager_plan = key, plan
        # Captured fills bind this call's stable pointers on every replay. A
        # subsequent eager call must not change another captured call's target.
        plan.source.bases[0].fill_(src_ptr or source.data_ptr())
        plan.target.bases[self.target_slot].fill_(dst_ptr or target.data_ptr())
        if target_view is not None:
            layout = target_view.layout
            locations = target_view.owner.write_locations[
                target_view.rank, target_view.region
            ]
            chunks = layout.row_bytes // row_bytes
            logical = target_rows // chunks
            handles = logical // target_view.token_capacity
            positions = logical % target_view.token_capacity
            logical_page = positions // layout.page_size
            regions = locations.regions[handles, logical_page]
            pages = locations.pages[handles, logical_page]
            enabled = locations.valid[handles, logical_page] & valid[:, None]
            physical = (
                pages * layout.layers * layout.page_size
                + (positions % layout.page_size)[:, None]
            ) * chunks + (target_rows % chunks)[:, None]
            starts = plan.target.ranges[regions, 0]
            plan.submit(
                source_rows[:, None].expand(-1, 2).flatten(),
                (starts + physical).flatten(),
                enabled.flatten(),
            )
        else:
            plan.submit(
                source_rows, target_rows + plan.target.starts[self.target_slot], valid
            )


class TensorCopy:
    """Tensor dimension convenience API shared by all host/HBM copy categories."""

    def __init__(self, kernel: Any = None) -> None:
        self.kernel = kernel
        self.copies: dict[tuple[str, int], IndexedCopy] = {}

    def __call__(
        self,
        source: Tensor,
        target: Tensor,
        source_rows: Tensor,
        target_rows: Tensor,
        valid: Tensor,
        src_dims: int,
        dst_dims: int,
        *,
        block_dim: int = 48,
        src_ptr: int | None = None,
        dst_ptr: int | None = None,
        copy_key: tuple[str, int],
    ) -> None:
        from math import prod

        width = prod(source.shape[src_dims:]) * source.element_size()
        # Separate simultaneous hit/refill/map streams and layer backing stores.
        copier = self.copies.setdefault(copy_key, IndexedCopy(self.kernel))
        copier(
            source,
            target,
            source_rows,
            target_rows,
            valid,
            prod(source.shape[:src_dims]),
            prod(target.shape[:dst_dims]),
            width,
            source_rows.numel(),
            block_dim,
            src_ptr,
            dst_ptr,
        )
