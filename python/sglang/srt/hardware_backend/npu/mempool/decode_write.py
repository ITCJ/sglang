"""Partition decode writes and keep the current token local to its producer."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import torch
from torch import Tensor

from .manager import MempoolKVManager


@dataclass(frozen=True)
class DecodeWriter:
    member: int
    members: int
    assignment: str = "features"

    @property
    def feature_part(self) -> tuple[int, int]:
        return (self.member, self.members) if self.assignment == "features" else (0, 1)

    def owns_layer(self, layer: int) -> bool:
        return self.assignment == "features" or layer % self.members == self.member


class DecodeKV:
    """Stable HBM rows and graph-captured evidence for one shared D replica.

    The real writer runs on the compute stream before post-attention TP
    communication. Only previous tokens may be read from shared DRAM.
    """

    def __init__(
        self,
        manager: MempoolKVManager,
        writer: DecodeWriter,
        rows: int,
        device: str,
        verify: bool,
        kernel: Any = None,
    ) -> None:
        self.manager, self.writer, self.verify = manager, writer, verify
        self.kernel = kernel
        layout = manager.layout.decode
        self.current = torch.zeros(
            (layout.layers, rows, layout.heads, layout.dim),
            dtype=torch.bfloat16,
            device=device,
        )
        # Per layer/request: checked prior rows, mismatches, physical bytes,
        # current-token HBM reads. No host reads occur during capture/replay.
        self.checks = torch.zeros(
            (layout.layers, rows, 4), dtype=torch.int64, device=device
        )
        self.dtype = torch.empty(1, dtype=torch.bfloat16, device="cpu")

    def copy(self, *args: Any) -> None:
        if self.kernel is None:
            importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
            self.kernel = torch.ops.npu.unidex_copy
        self.kernel(*args)

    def prepare(
        self,
        layer: int,
        values: Tensor,
        rows: Tensor,
        slots: Tensor,
        positions: Tensor,
        valid: Tensor,
    ) -> None:
        view = self.manager.view(1, layer)
        layout = view.layout
        if self.verify:
            previous = valid & (positions > 0)
            actual = torch.empty_like(values)
            self.copy(
                self.dtype,
                actual,
                torch.where(previous, slots * layout.tokens + positions - 1, 0),
                torch.arange(rows.numel(), device=rows.device),
                previous,
                layout.slots * layout.tokens,
                rows.numel(),
                layout.row_bytes,
                rows.numel(),
                48,
                view.device_base,
                None,
            )
            wrong = previous & (actual != self.current[layer, rows]).flatten(1).any(1)
            self.checks[layer, :, 0].scatter_add_(0, rows, previous.long())
            self.checks[layer, :, 1].scatter_add_(0, rows, wrong.long())
        self.current[layer].index_copy_(0, rows, values)
        byte_count = (
            layout.row_bytes // self.writer.feature_part[1]
            if self.writer.owns_layer(layer)
            else 0
        )
        self.checks[layer, :, 2].scatter_add_(0, rows, valid.long() * byte_count)

    def supply_current(
        self,
        layer: int,
        rows: Tensor,
        positions: Tensor,
        misses: Tensor,
        output: Tensor,
        lengths: Tensor,
    ) -> Tensor:
        safe = torch.where((rows > 0) & (rows < lengths.numel()), rows, 0).long()
        current = misses & (rows > 0)[:, None] & (positions == lengths[safe, None] - 1)
        # Run on the existing miss stream after copy_ready. The indexed kernel
        # touches only current-token misses; the hit stream owns other rows.
        self.copy(
            self.current[layer],
            output,
            safe[:, None].expand_as(positions).flatten(),
            torch.arange(positions.numel(), dtype=torch.int64, device=rows.device),
            current.flatten(),
            self.current.shape[1],
            positions.numel(),
            self.manager.layout.decode.row_bytes,
            positions.numel(),
            48,
            None,
            None,
        )
        self.checks[layer, :, 3].index_add_(0, safe, current.sum(1))
        return misses & ~current
