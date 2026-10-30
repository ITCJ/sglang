"""Acceptance-only cross-writer reads through the live shared prompt mapping."""

from __future__ import annotations

import importlib
from typing import Any

from .manager import MempoolKVManager
from .page_write import PageWriter


class SharedPromptProbe:
    """Use one row per writer in a free slot before Main admits any request."""

    def __init__(
        self, manager: MempoolKVManager, page_size: int, assignment: str, device: str
    ) -> None:
        self.manager, self.device = manager, device
        session = manager.session
        assert session is not None
        self.members = session.members
        self.rank = session.tp_rank
        view = manager.view(0, 0)
        self.view = view
        self.slot = view.layout.slots - 1
        self.positions = [
            PageWriter(i, len(self.members), page_size, assignment).pages(
                view.layout.tokens
            )[0]
            * page_size
            for i in range(len(self.members))
        ]

    def write(self) -> None:
        import torch

        if self.manager.rank == 0:
            values = torch.full(
                (1, self.view.layout.heads, self.view.layout.dim),
                self.rank + 1,
                dtype=torch.bfloat16,
                device=self.device,
            )
            self.view.write_rows(
                self.slot, self.positions[self.members.index(self.rank)], values
            )

    def verify(self) -> dict[str, Any]:
        """Run only after the existing TP tick has gathered all writers' registration."""
        import torch

        importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
        layout, count = self.view.layout, len(self.members)
        values = torch.empty(
            (count, layout.heads, layout.dim), dtype=torch.bfloat16, device=self.device
        )
        source = torch.tensor(
            [self.slot * layout.tokens + p for p in self.positions],
            dtype=torch.int64,
            device=self.device,
        )
        destination = torch.arange(count, dtype=torch.int64, device=self.device)
        valid = torch.ones(count, dtype=torch.bool, device=self.device)
        torch.ops.npu.unidex_copy(
            torch.empty(1, dtype=torch.bfloat16, device="cpu"),
            values,
            source,
            destination,
            valid,
            layout.slots * layout.tokens,
            count,
            layout.row_bytes,
            count,
            48,
            self.view.device_base,
            None,
        )
        actual = values.cpu()
        for member, writer in enumerate(self.members):
            if not bool((actual[member] == writer + 1).all()):
                raise RuntimeError(
                    f"shared prompt probe: reader {self.rank} cannot read writer {writer}"
                )
        return dict(
            status="passed",
            reader=self.rank,
            writers=list(self.members),
            positions=self.positions,
            slot=self.slot,
            bytes=count * layout.row_bytes,
            copy="unidex_copy",
            source="shared_prompt",
            phase="before_admission",
        )
