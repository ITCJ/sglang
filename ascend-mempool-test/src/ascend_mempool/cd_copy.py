"""Pure copy paths for the fixed-shape C/D matrix; no materialize or collectives."""

from __future__ import annotations

from typing import Any

from .cd_plan import BLOCK_DIM, TOPK, CDCase, indices, write_span
from .sharing_copy import pattern_rows


def write_values(values: Any, variant: int) -> Any:
    import torch

    # Keep every marker in [-256,-1], exactly representable in BF16. Adding a
    # generation directly to a BF16 value near 256 would merge adjacent rows.
    encoded = values.to(torch.int16).bitwise_xor((variant + 17) % 256) + 1
    return -encoded.to(torch.bfloat16)


class CDWorkload:
    def __init__(
        self,
        pool: Any,
        case: CDCase,
        *,
        seed: int,
        device: str = "npu",
        kernel: Any = None,
        topk: int = TOPK,
    ) -> None:
        import torch

        self.pool, self.case, self.seed = pool, case, seed
        self.device, self.topk = device, topk
        self.kernel = kernel if kernel is not None else torch.ops.npu.unidex_copy
        self.layout = pool.layout.layout_for_rank(pool.rank)
        self.dtype = torch.empty(1, dtype=torch.bfloat16)
        self.variant = 0
        self.routes = []
        if case.operation == "read":
            count = case.active * topk
            self.output = torch.empty(
                (count, 1, self.layout.dim), dtype=torch.bfloat16, device=device
            )
            for side in (0, 1):
                self.routes.append(
                    (
                        torch.zeros(count, dtype=torch.int64, device=device),
                        torch.arange(count, dtype=torch.int64, device=device),
                        torch.zeros(count, dtype=torch.bool, device=device),
                    )
                )
            self.payload = count * self.layout.row_bytes
        else:
            self.begin, self.end = write_span(
                case.rows, pool.member, pool.tp_size, pool.mode, case.operation
            )
            count = self.end - self.begin
            self.values = torch.empty(
                (count, 1, self.layout.dim), dtype=torch.bfloat16, device=device
            )
            self.src = torch.arange(count, dtype=torch.int64, device=device)
            # Row 0 and row rows+1 are guards; all writers address slot 0.
            self.dst = self.src + self.begin + 1
            self.valid = torch.ones(count, dtype=torch.bool, device=device)
            self.payload = count * self.layout.row_bytes
        self.prepare(0)

    def prepare(self, variant: int) -> None:
        import torch

        self.variant = variant
        if self.case.operation != "read":
            tokens = torch.arange(self.begin + 1, self.end + 1)
            values = pattern_rows(
                self.pool.rank, 0, torch.zeros_like(tokens), tokens, self.layout.dim
            )
            self.values.copy_(write_values(values, variant).to(self.device))
            return
        self.tokens = torch.tensor(
            indices(self.case, variant, self.seed, self.layout.tokens, self.topk),
            dtype=torch.int64,
        )
        self.slots = (torch.arange(self.case.active) + variant) % self.layout.slots
        if self.case.source == "mixed":
            self.sources = (torch.arange(self.topk) % 2).expand(self.case.active, -1)
        else:
            side = self.pool.rank if self.case.source == "local" else 1 - self.pool.rank
            self.sources = torch.full_like(self.tokens, side)
        source_rows = self.slots[:, None] * self.layout.tokens + self.tokens
        for side, (src, _, valid) in enumerate(self.routes):
            src.copy_(source_rows.flatten().to(self.device))
            valid.copy_((self.sources == side).flatten().to(self.device))

    def launch(self) -> None:
        if self.case.operation == "read":
            for side, (src, dst, valid) in enumerate(self.routes):
                self.kernel(
                    self.dtype,
                    self.output,
                    src,
                    dst,
                    valid,
                    self.layout.slots * self.layout.tokens,
                    self.output.shape[0],
                    self.layout.row_bytes,
                    src.numel(),
                    BLOCK_DIM,
                    self.pool.view(side, 0).device_base,
                    None,
                )
        else:
            self.kernel(
                self.values,
                self.dtype,
                self.src,
                self.dst,
                self.valid,
                self.end - self.begin,
                self.layout.slots * self.layout.tokens,
                self.layout.row_bytes,
                self.end - self.begin,
                BLOCK_DIM,
                None,
                self.pool.view(self.pool.rank, 0).device_base,
            )

    def reset(self) -> None:
        """Restore canonical values outside timing; only the physical owner writes."""
        import torch

        if self.case.operation == "read" or (
            self.pool.mode == "shared" and self.pool.member != 0
        ):
            return
        tokens = torch.arange(self.case.rows + 2)
        values = pattern_rows(
            self.pool.rank, 0, torch.zeros_like(tokens), tokens, self.layout.dim
        ).to(self.device)
        self.pool.view(self.pool.rank, 0).write_rows(0, 0, values)

    def verify(self) -> None:
        import torch

        if self.case.operation == "read":
            expected = torch.empty_like(self.output, device="cpu")
            tokens = self.tokens.flatten()
            slots = self.slots[:, None].expand_as(self.tokens).flatten()
            for side in (0, 1):
                mask = self.sources.flatten() == side
                expected[mask] = pattern_rows(
                    side, 0, slots[mask], tokens[mask], self.layout.dim
                )
            actual = self.output.cpu()
        else:
            count = self.case.rows + 2
            idx = torch.arange(count, dtype=torch.int64, device=self.device)
            dst = torch.empty(
                (count, 1, self.layout.dim), dtype=torch.bfloat16, device=self.device
            )
            self.kernel(
                self.dtype,
                dst,
                idx,
                idx,
                torch.ones_like(idx, dtype=torch.bool),
                self.layout.slots * self.layout.tokens,
                count,
                self.layout.row_bytes,
                count,
                BLOCK_DIM,
                self.pool.view(self.pool.rank, 0).device_base,
                None,
            )
            tokens = torch.arange(count)
            expected = pattern_rows(
                self.pool.rank, 0, torch.zeros_like(tokens), tokens, self.layout.dim
            )
            begin, end = (
                (self.begin, self.end)
                if self.pool.mode == "independent"
                else (0, self.case.rows)
            )
            expected[begin + 1 : end + 1] = write_values(
                expected[begin + 1 : end + 1], self.variant
            )
            actual = dst.cpu()
        if not torch.equal(actual, expected):
            first = actual.ne(expected).nonzero()[0].tolist()
            raise AssertionError(f"{self.case.name}: copy mismatch at {first}")
