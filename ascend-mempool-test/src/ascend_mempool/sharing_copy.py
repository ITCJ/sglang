"""Real UniDexCopy workloads with an independent host content oracle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .sharing import writer_rows
from .sharing_cases import Case
from .verification import kv_pattern

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.copy import (
        SparseCopyInputs,
        SparseKVCopy,
    )
else:
    from .copy import SparseCopyInputs, SparseKVCopy


def pattern_rows(side: int, layer: int, slots: Any, tokens: Any, dim: int) -> Any:
    """Vectorized host oracle; coordinates come from the requested logical KV."""
    import torch

    slots, tokens = slots.cpu().reshape(-1, 1), tokens.cpu().reshape(-1, 1)
    columns = torch.arange(dim).reshape(1, -1)
    features = (
        torch.full_like(tokens, side + 1),
        torch.full_like(tokens, layer % 256),
        slots % 256,
        tokens % 256,
        (tokens // 256) % 256,
        (tokens // 65536) % 256,
        columns % 256,
        (layer * 11 + slots * 7 + columns // 256) % 256,
    )
    values = torch.zeros((tokens.numel(), dim), dtype=torch.float32)
    for channel, feature in enumerate(features):
        values = torch.where(columns % 8 == channel, feature.float(), values)
    return values.to(torch.bfloat16).unsqueeze(1)


class CopyWorkload:
    """Stable source, destination and metadata for eager and repeated Graph use."""

    def __init__(
        self,
        pool: Any,
        case: Case,
        *,
        topk: int,
        block_dim: int,
        device: str = "npu",
        kernel: Any = None,
        side_active: bool = True,
    ) -> None:
        import torch

        self.pool, self.case, self.topk, self.block_dim = pool, case, topk, block_dim
        self.tp_size = pool.tp_size
        self.device = device
        self.kernel = kernel if kernel is not None else torch.ops.npu.unidex_copy
        self.batch = min(16, pool.layout.prompt.slots)
        if case.active > self.batch:
            raise ValueError("active requests exceed physical slots")
        self.layer = case.layer
        self.inputs = SparseCopyInputs(self.batch, topk, device)
        self.copy = SparseKVCopy(
            pool.view(0, self.layer),
            pool.view(1, self.layer),
            self.inputs,
            block_dim=block_dim,
            kernel=self.kernel,
        )
        self.output = self.copy.output
        self.dtype_placeholder = torch.empty(1, dtype=torch.bfloat16)
        self.write_count = max(1, case.active) * case.rows
        self.values = torch.empty(
            (self.write_count, 1, pool.layout.prompt.dim),
            dtype=torch.bfloat16,
            device=device,
        )
        self.write_src = torch.arange(
            self.write_count, dtype=torch.int64, device=device
        )
        self.write_dst = torch.zeros_like(self.write_src)
        self.write_valid = torch.zeros(
            self.write_count, dtype=torch.bool, device=device
        )
        self.feature_src = torch.empty_like(self.write_src)
        self.feature_dst = torch.empty_like(self.write_dst)
        self.current_src = torch.zeros(
            self.batch * topk, dtype=torch.int64, device=device
        )
        self.current_valid = torch.zeros(
            self.batch * topk, dtype=torch.bool, device=device
        )
        self.current_dst = self.inputs.dst_index
        self.current_values = torch.empty(
            (self.batch, 1, pool.layout.prompt.dim), dtype=torch.bfloat16, device=device
        )
        self.has_write = case.operation in (
            "logical",
            "equal_bytes",
            "write_then_read",
            "write_with_history",
            "current_serial",
            "current_parallel",
        )
        self.has_read = case.operation not in ("logical", "equal_bytes")
        self.has_current = case.operation in (
            "current_serial",
            "current_parallel",
            "write_with_history",
        )
        self.selects_current = self.has_current or case.operation == "write_then_read"
        self.side_active = side_active
        self.enabled = side_active and (
            case.accessor == -1 or case.accessor == pool.member
        )
        self.variant = 0
        self.routes: tuple[Any, Any]
        self.load(0)

    @property
    def write_extent(self) -> int:
        return self.case.rows * (
            self.tp_size
            if self.case.operation == "equal_bytes" and self.pool.mode == "shared"
            else 1
        )

    def load(self, variant: int, *, empty: bool = False) -> None:
        """Update existing tensors; a capture with empty masks can later do real work."""
        import torch

        self.variant, self.empty = variant, empty
        case, pool, inputs = self.case, self.pool, self.inputs
        p, d = pool.layout.prompt, pool.layout.decode
        local = pool.layout.layout_for_rank(pool.rank)
        row_bytes = local.row_bytes
        extent = self.write_extent
        self.first_write = local.tokens - extent - 1
        if self.first_write < 1:
            raise ValueError("write plus guard rows exceeds per-slot capacity")
        reqs = torch.arange(self.batch)
        p_slots, d_slots = (reqs + variant) % p.slots, (reqs + variant + 1) % d.slots
        inputs.p_slots.copy_(p_slots)
        inputs.d_slots.copy_(d_slots)
        inputs.prompt_lengths.fill_(p.tokens)
        inputs.decode_lengths.fill_(d.tokens)
        active = (reqs < case.active) & self.enabled & (not empty)
        inputs.active.copy_(active)
        positions = torch.arange(self.topk).expand(self.batch, -1).clone()
        use_p = torch.ones_like(positions, dtype=torch.bool)
        if case.source == "d":
            use_p.fill_(False)
        elif case.source == "mixed":
            use_p = positions % 2 == 0
        source_ordinal = positions // 2 if case.source == "mixed" else positions
        p_window, d_window = p.tokens // 2, d.tokens // 2
        if self.has_read and min(p_window, d_window) <= self.topk:
            raise ValueError("read history must exceed the full effective top-k")
        stride = 2053 if case.distribution == "scattered" else 1
        p_tokens = (source_ordinal * stride + variant * 7) % p_window
        d_tokens = (source_ordinal * stride + variant * 11) % d_window
        positions = torch.where(use_p, p_tokens, p.tokens + d_tokens)
        if self.selects_current:
            # Deliberately put the current token at non-tail columns, twice.
            positions[:, 3] = self.first_write + (p.tokens if pool.rank else 0)
            positions[:, self.topk // 2] = positions[:, 3]
        self.host_positions = positions
        self.host_slots = (p_slots, d_slots)
        inputs.positions.copy_(positions)
        inputs.valid.fill_(not empty)
        current = (
            (positions == self.first_write + (p.tokens if pool.rank else 0))
            & active.unsqueeze(1)
            & self.selects_current
        )
        local_current = current & self.has_current
        self.current_valid.copy_(local_current.flatten())
        self.current_src.copy_(reqs.unsqueeze(1).expand(-1, self.topk).flatten())
        # Historical masks exclude current before either stream is submitted.
        inputs.valid.logical_and_(~local_current.to(device=self.device))
        routes = self.copy.routes()
        if hasattr(self, "routes"):
            for target, new in zip(self.routes, routes):
                target.src_index.copy_(new.src_index)
                target.valid.copy_(new.valid)
        else:
            self.routes = routes
        local_slots = (p_slots, d_slots)[pool.rank]
        self.write_slots_host = local_slots[: max(1, case.active)].repeat_interleave(
            case.rows
        )
        offset = torch.arange(case.rows)
        if case.operation == "equal_bytes" and pool.mode == "shared":
            offset += pool.member * case.rows
        self.write_tokens_host = (offset + self.first_write).repeat(max(1, case.active))
        values = pattern_rows(
            pool.rank,
            self.layer,
            self.write_slots_host,
            self.write_tokens_host,
            local.dim,
        )
        values = -values - (variant + 8)
        self.values.copy_(values)
        self.write_dst.copy_(
            self.write_slots_host * local.tokens + self.write_tokens_host
        )
        self.feature_src.copy_(self.write_src * self.tp_size + pool.member)
        self.feature_dst.copy_(self.write_dst * self.tp_size + pool.member)
        ownership = torch.ones(self.write_count, dtype=torch.bool)
        if (
            pool.mode == "shared"
            and case.operation != "equal_bytes"
            and case.accessor == -1
        ):
            if case.partition == "token":
                ownership.zero_()
                ownership[
                    writer_rows(
                        "shared", "logical", self.write_count, pool.member, self.tp_size
                    )
                ] = True
            elif case.partition == "layer":
                ownership.fill_(self.layer % self.tp_size == pool.member)
        ownership &= self.enabled and not empty and self.has_write
        self.write_valid.copy_(ownership)
        current_values = pattern_rows(
            pool.rank,
            self.layer,
            local_slots,
            torch.full((self.batch,), self.first_write),
            local.dim,
        )
        current_values = -current_values - (variant + 8)
        self.current_values.copy_(current_values)
        self.expected = torch.zeros(self.output.shape, dtype=torch.bfloat16)
        for row in range(case.active if self.enabled and not empty else 0):
            for side in (0, 1):
                mask = (
                    (positions[row] < p.tokens)
                    if side == 0
                    else (positions[row] >= p.tokens)
                )
                tokens = positions[row, mask] - (p.tokens if side else 0)
                slots = torch.full_like(tokens, self.host_slots[side][row])
                self.expected[row, mask] = pattern_rows(
                    side, self.layer, slots, tokens, local.dim
                )
            self.expected[row, current[row]] = current_values[row]
        self.expected_current = current
        write_units = int(ownership.sum())
        feature = (
            pool.mode == "shared"
            and case.partition == "feature"
            and case.accessor == -1
        )
        self.feature_split = feature
        self.write_payload = write_units * row_bytes // (self.tp_size if feature else 1)
        self.read_payload = (
            int((active.sum() * self.topk) - local_current.sum()) * row_bytes
            if self.has_read
            else 0
        )
        self.current_payload = int(local_current.sum()) * row_bytes
        self.logical_bytes = (
            case.rows * max(1, case.active) * row_bytes
            if self.enabled
            and not empty
            and self.has_write
            and case.operation != "equal_bytes"
            else 0
        )
        self.output.zero_()

    def write(self) -> None:
        local = self.pool.layout.layout_for_rank(self.pool.rank)
        if self.feature_split:
            self.kernel(
                self.values,
                self.dtype_placeholder,
                self.feature_src,
                self.feature_dst,
                self.write_valid,
                self.write_count * self.tp_size,
                local.slots * local.tokens * self.tp_size,
                local.row_bytes // self.tp_size,
                self.write_count,
                self.block_dim,
                None,
                self.pool.view(self.pool.rank, self.layer).device_base,
            )
        else:
            self.kernel(
                self.values,
                self.dtype_placeholder,
                self.write_src,
                self.write_dst,
                self.write_valid,
                self.write_count,
                local.slots * local.tokens,
                local.row_bytes,
                self.write_count,
                self.block_dim,
                None,
                self.pool.view(self.pool.rank, self.layer).device_base,
            )

    def history(self) -> None:
        for side, route in enumerate(self.routes):
            source = self.pool.view(side, self.layer)
            self.kernel(
                self.dtype_placeholder,
                self.output,
                route.src_index,
                route.dst_index,
                route.valid,
                source.layout.slots * source.layout.tokens,
                self.batch * self.topk,
                source.layout.row_bytes,
                self.batch * self.topk,
                self.block_dim,
                source.device_base,
                None,
            )

    def current(self) -> None:
        self.kernel(
            self.current_values,
            self.output,
            self.current_src,
            self.current_dst,
            self.current_valid,
            self.batch,
            self.batch * self.topk,
            self.pool.layout.prompt.row_bytes,
            self.batch * self.topk,
            self.block_dim,
            None,
            None,
        )

    def verify_selected(self) -> None:
        import torch

        if self.has_read and not torch.equal(self.output.cpu(), self.expected):
            index = tuple(self.output.cpu().ne(self.expected).nonzero()[0].tolist())
            raise AssertionError(f"{self.case.name} selected mismatch at {index}")

    def reset_write_region(self, torch: Any) -> None:
        """The physical owner initializes guards, outside all measurements."""
        if not self.side_active or (
            self.pool.mode == "shared" and self.pool.member != 0
        ):
            return
        layout = self.pool.layout.layout_for_rank(self.pool.rank)
        extent = self.write_extent
        for slot in range(layout.slots):
            values = kv_pattern(
                self.pool.rank,
                self.layer,
                slot,
                self.first_write - 1,
                extent + 2,
                1,
                layout.dim,
            ).to(self.device)
            self.pool.view(self.pool.rank, self.layer).write_rows(
                slot, self.first_write - 1, values
            )

    def verify_writes(self, torch: Any) -> None:
        """All members independently read complete modified rows AND untouched guards."""
        if not self.side_active or not self.has_write:
            return
        layout = self.pool.layout.layout_for_rank(self.pool.rank)
        extent = self.write_extent
        count = extent + 2
        source = self.pool.view(self.pool.rank, self.layer)
        dst = torch.empty(
            (count, 1, layout.dim), dtype=torch.bfloat16, device=self.device
        )
        indices = torch.arange(count, dtype=torch.int64, device=self.device)
        valid = torch.ones(count, dtype=torch.bool, device=self.device)
        for slot in range(layout.slots):
            self.kernel(
                self.dtype_placeholder,
                dst,
                indices + slot * layout.tokens + self.first_write - 1,
                indices,
                valid,
                layout.slots * layout.tokens,
                count,
                layout.row_bytes,
                count,
                self.block_dim,
                source.device_base,
                None,
            )
            actual = dst.cpu()
            expected = kv_pattern(
                self.pool.rank,
                self.layer,
                slot,
                self.first_write - 1,
                count,
                1,
                layout.dim,
            )
            is_target = (
                slot
                in self.host_slots[self.pool.rank][: max(1, self.case.active)].tolist()
            )
            writes = (
                not self.empty
                and is_target
                and (self.enabled or self.pool.mode == "shared")
            )
            if writes:
                expected[1:-1] = -expected[1:-1] - (self.variant + 8)
            if not torch.equal(actual, expected):
                raise AssertionError(
                    f"{self.case.name}: incomplete write or overwritten guard, slot={slot}"
                )

    def byte_counts(self) -> dict[str, Any]:
        selected = (
            (self.case.active if self.enabled and not self.empty else 0)
            * self.topk
            * self.pool.layout.prompt.row_bytes
        )
        materialize = self.case.operation == "materialize"
        hit = selected - self.read_payload if materialize else 0
        refill = selected if materialize else 0
        slot_map = (
            (self.case.active if self.enabled and not self.empty else 0) * self.topk * 4
            if materialize
            else 0
        )
        return dict(
            write_bytes=self.write_payload,
            bm_read_bytes=self.read_payload,
            current_bytes=self.current_payload,
            hbm_hit_bytes=hit,
            refill_bytes=refill,
            slot_map_bytes=slot_map,
            bm_copy_bytes=self.write_payload + self.read_payload,
            copy_bytes=self.write_payload
            + self.read_payload
            + self.current_payload
            + hit
            + refill
            + slot_map,
            logical_bytes=self.logical_bytes,
            valid_write_units=int(self.write_valid.cpu().sum()),
            partition=self.case.partition,
            block_dim=self.block_dim,
            feature_span_bytes=self.pool.layout.prompt.row_bytes // self.tp_size
            if self.feature_split
            else None,
        )
