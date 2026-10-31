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
    """Stable HBM rows for the current token of one shared D replica.

    The real writer runs on the compute stream before post-attention TP
    communication. Only previous tokens may be read from shared DRAM.
    """

    def __init__(
        self,
        manager: MempoolKVManager,
        writer: DecodeWriter,
        rows: int,
        device: str,
        kernel: Any = None,
    ) -> None:
        self.manager, self.writer = manager, writer
        self.kernel = kernel
        layout = manager.layout.decode
        self.current = torch.zeros(
            (layout.layers, rows, layout.heads, layout.dim),
            dtype=torch.bfloat16,
            device=device,
        )

    def copy(self, *args: Any) -> None:
        if self.kernel is None:
            importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
            self.kernel = torch.ops.npu.unidex_copy
        self.kernel(*args)

    def save_current(self, layer: int, values: Tensor, rows: Tensor) -> None:
        self.current[layer].index_copy_(0, rows, values)

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
        return misses & ~current
