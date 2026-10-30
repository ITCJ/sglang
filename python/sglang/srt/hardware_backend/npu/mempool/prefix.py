"""Retain prompt pages independently of alive slots and native request rows."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
import time
from contextlib import nullcontext
from typing import Any, Callable

import msgspec
import torch
from torch import Tensor

from .layout import check_index, positive_int
from .manager import MempoolKVManager
from .restore_timing import RestoreTiming
from .split_read import assign_pages

logger = logging.getLogger(__name__)


class _Source(msgspec.Struct):
    rid: str
    slot: int
    tokens: int
    values: dict[tuple[str, int], Tensor] = msgspec.field(default_factory=dict)
    coverage: dict[tuple[str, int], Tensor] = msgspec.field(default_factory=dict)
    verified: bool = False


class _Admit(msgspec.Struct):
    rid: str
    op_id: int
    slot: int
    prompt_tokens: int
    source_pages: tuple[int, ...]
    target_pages: tuple[int, ...]
    event: Any = None
    retained: list[Tensor] = msgspec.field(default_factory=list)
    receipt: dict[str, Any] | None = None


class _Read(msgspec.Struct):
    rid: str
    op_id: int
    kind: str
    source_pages: tuple[int, ...]
    slot: int
    indices: Tensor | None = None
    pool: Any = None
    event: Any = None
    retained: list[Tensor] = msgspec.field(default_factory=list)
    receipt: dict[str, Any] | None = None
    split_read: dict[str, Any] = msgspec.field(default_factory=dict)
    timing: Any = None
    restore_buffers: dict[str, Tensor] = msgspec.field(default_factory=dict)
    oracle_indices: tuple[Tensor, Tensor, Tensor] | None = None


class PrefixStore:
    """Own P's local page reservations and copies, with receipts after NPU completion."""

    def __init__(
        self,
        manager: MempoolKVManager,
        *,
        page_size: int,
        device: str,
        kernel: Any = None,
        event_factory: Callable[[], Any] | None = None,
        verify: bool = False,
        replica: int = 0,
        readers: int = 1,
        read_assignment: str = "round_robin",
        all_gather: Callable[[Tensor, Tensor], None] | None = None,
        scatter_kernel: Any = None,
        profile: bool = False,
    ) -> None:
        positive_int("page_size", page_size)
        layout = manager.layout.prefix_main
        if manager.rank != 0 or layout is None or layout.tokens != page_size:
            raise ValueError("prefix store requires P's configured page regions")
        self.manager, self.page_size, self.device = manager, page_size, device
        self.verify = verify
        self.replica = replica
        self.readers, self.read_assignment, self.all_gather = (
            readers,
            read_assignment,
            all_gather,
        )
        self._kernel = kernel
        self._scatter_kernel = scatter_kernel
        self.profile = profile
        self._restore_logged = False
        self._event_factory = event_factory or (lambda: torch.npu.Event())
        self._free = set(range(layout.slots))
        self._operations: dict[int, _Admit] = {}
        self._reads: dict[int, _Read] = {}
        self._ready: set[int] = set()
        self._read_stream: Any = None
        self._fault: str | None = None
        self._dtype = torch.empty(1, dtype=torch.bfloat16, device="cpu")
        # Acceptance-only CPU references: one current request plus retained pages.
        self._source: _Source | None = None
        self._saved: dict[int, dict[tuple[str, int], Tensor]] = {}

    @property
    def free_pages(self) -> tuple[int, ...]:
        return tuple(sorted(self._free))

    def admit(
        self,
        *,
        rid: str,
        op_id: int,
        slot: int,
        prompt_tokens: int,
        source_pages: list[int],
        target_pages: list[int],
    ) -> dict[str, Any] | None:
        """Submit once, then poll the same operation without releasing its source."""
        if self._fault is not None:
            raise RuntimeError(f"prefix store fault: {self._fault}")
        candidate = _Admit(
            rid=rid,
            op_id=op_id,
            slot=slot,
            prompt_tokens=prompt_tokens,
            source_pages=tuple(source_pages),
            target_pages=tuple(target_pages),
        )
        operation = self._operations.get(op_id)
        if operation is None:
            self._validate(candidate)
            self._operations[op_id] = operation = candidate
            self._free.difference_update(target_pages)
            try:
                self._submit(operation)
            except Exception as exc:
                self._fault = str(exc)
                raise
        elif any(
            getattr(operation, name) != getattr(candidate, name)
            for name in ("rid", "slot", "prompt_tokens", "source_pages", "target_pages")
        ):
            raise ValueError("conflicting duplicate prefix operation")
        if operation.receipt is None:
            if not operation.event.query():
                return None
            main, index = (
                self.manager.layout.prefix_main,
                self.manager.layout.prefix_index,
            )
            assert main is not None and index is not None
            if self.verify:
                self._verify_admit(operation)
            operation.receipt = dict(
                readable=True,
                slot=slot,
                source_pages=source_pages.copy(),
                target_pages=target_pages.copy(),
                main_layers=main.layers,
                index_layers=index.layers,
            )
            operation.retained.clear()
            self._ready.update(target_pages)
        return copy.deepcopy(operation.receipt)

    def _read_context(self) -> Any:
        if torch.device(self.device).type == "cpu":
            return nullcontext()
        if self._read_stream is None:
            self._read_stream = torch.npu.Stream()
        self._read_stream.wait_stream(torch.npu.current_stream())
        return torch.npu.stream(self._read_stream)

    def fill(
        self, *, rid: str, op_id: int, slot: int, source_pages: list[int]
    ) -> dict[str, Any] | None:
        """Copy retained pages into this request's alive on an independent stream."""
        return self._read(_Read(rid, op_id, "fill", tuple(source_pages), slot))

    def load_back(
        self,
        *,
        rid: str,
        op_id: int,
        slot: int,
        source_pages: list[int],
        indices: Tensor,
        pool: Any,
    ) -> dict[str, Any] | None:
        """Restore both MLA parts and Index K at allocator-owned token indices."""
        return self._read(
            _Read(rid, op_id, "load_back", tuple(source_pages), slot, indices, pool)
        )

    def _read(self, candidate: _Read) -> dict[str, Any] | None:
        if self._fault is not None:
            raise RuntimeError(f"prefix store fault: {self._fault}")
        rid, op_id, slot, source_pages = (
            candidate.rid,
            candidate.op_id,
            candidate.slot,
            candidate.source_pages,
        )
        operation = self._reads.get(op_id)
        if operation is None:
            positive_int("op_id", op_id)
            check_index("target slot", slot, self.manager.layout.prompt.slots)
            if (
                not rid
                or not source_pages
                or len(set(source_pages)) != len(source_pages)
                or any(type(p) is not int or p not in self._ready for p in source_pages)
                or len(source_pages) * self.page_size
                > self.manager.layout.prompt.tokens
                or op_id in self._operations
            ):
                raise ValueError("prefix read requires unique ready source pages")
            self._reads[op_id] = operation = candidate
            try:
                with self._read_context():
                    if operation.kind == "fill":
                        self._submit_fill(operation)
                    else:
                        self._submit_load_back(operation)
            except Exception as exc:
                self._fault = str(exc)
                raise
        elif (
            any(
                getattr(operation, key) != getattr(candidate, key)
                for key in ("rid", "kind", "source_pages", "slot")
            )
            or operation.indices is not candidate.indices
            or operation.pool is not candidate.pool
        ):
            raise ValueError("conflicting duplicate prefix read")
        if operation.receipt is None:
            if not operation.event.query():
                return None
            completed_ns = time.monotonic_ns()
            timings = (
                operation.timing.finish()
                if self.profile and operation.indices is not None
                else {}
            )
            main, index = (
                self.manager.layout.prefix_main,
                self.manager.layout.prefix_index,
            )
            assert main is not None and index is not None
            if self.verify:
                self._verify_read(operation)
            operation.receipt = dict(
                readable=True,
                slot=slot,
                source_pages=list(source_pages),
                tokens=len(source_pages) * self.page_size,
                main_layers=main.layers,
                index_layers=index.layers,
            )
            if operation.indices is not None:
                operation.receipt["prefix_indices"] = operation.indices.cpu().tolist()
                operation.receipt["split_read"] = dict(
                    operation.split_read, completed_ns=completed_ns
                )
                if self.profile:
                    operation.receipt["split_read"]["timings_ms"] = timings
            operation.retained.clear()
            operation.restore_buffers.clear()
            operation.oracle_indices = None
        return copy.deepcopy(operation.receipt)

    def _verify_read(self, operation: _Read) -> None:
        source = self._source
        if source is None or (source.rid, source.slot) != (
            operation.rid,
            operation.slot,
        ):
            raise RuntimeError("prefix read has no matching content reference")
        references = {
            logical: self._saved[page]
            for logical, page in enumerate(operation.source_pages)
        }
        if operation.kind == "fill":
            first = operation.slot * (
                self.manager.layout.prompt.tokens // self.page_size
            )
            actual = self._check_pages(
                "alive", {first + p: ref for p, ref in references.items()}
            )
            # References come from A's original computed values, never B's destination.
            for logical, reference in references.items():
                for (region, layer), expected in reference.items():
                    self.capture_source(
                        region,
                        layer,
                        expected,
                        torch.full((self.page_size,), operation.slot),
                        torch.arange(
                            logical * self.page_size, (logical + 1) * self.page_size
                        ),
                        torch.ones(self.page_size, dtype=torch.bool),
                    )
            self._report(
                "fill",
                source,
                operation.op_id,
                list(operation.source_pages),
                {str(int(p) - first): digest for p, digest in actual.items()},
            )
            return
        pool, indices = operation.pool, operation.indices
        assert indices is not None
        actual_pages: dict[int, dict[tuple[str, int], Tensor]] = {
            p: {} for p in references
        }
        for region, layer in references[0]:
            expected = torch.cat([ref[region, layer] for ref in references.values()])
            private_values = self._read_private_prefix(operation, region, layer)
            if not torch.equal(expected, private_values):
                raise RuntimeError("load_back private prefix differs from saved source")
            cpu_indices = indices.cpu()
            if region == "main":
                rows = []
                for buffer, dim in (
                    (pool.get_key_buffer(pool.start_layer + layer), pool.kv_lora_rank),
                    (
                        pool.get_value_buffer(pool.start_layer + layer),
                        pool.qk_rope_head_dim,
                    ),
                ):
                    host = buffer.cpu()
                    if pool.use_fia_nz:
                        host = host.view(-1, dim // 16, self.page_size, 16).permute(
                            0, 2, 1, 3
                        )
                        rows.append(
                            host[
                                cpu_indices // self.page_size,
                                cpu_indices % self.page_size,
                            ].reshape(-1, 1, dim)
                        )
                    else:
                        rows.append(host.view(-1, 1, dim)[cpu_indices])
                k, rope = rows
                if not torch.equal(
                    k, expected[..., : pool.kv_lora_rank]
                ) or not torch.equal(rope, expected[..., pool.kv_lora_rank :]):
                    raise RuntimeError("load_back native main KV content mismatch")
                values = torch.cat((k, rope), dim=-1)
            else:
                values = (
                    pool.get_index_k_buffer(pool.indexer_layer_ids[layer])
                    .cpu()
                    .reshape(-1, 1, pool.index_head_dim)
                    .index_select(0, cpu_indices)
                )
                if not torch.equal(values, expected):
                    raise RuntimeError("load_back native Index K content mismatch")
            for logical in references:
                actual_pages[logical][region, layer] = values[
                    logical * self.page_size : (logical + 1) * self.page_size
                ]
        operation.split_read.update(native_verified=True, private_replica_verified=True)
        digests = {}
        for logical, page in actual_pages.items():
            digest = hashlib.sha256()
            for _, values in sorted(page.items()):
                digest.update(values.contiguous().view(torch.uint8).numpy().tobytes())
            digests[str(logical)] = digest.hexdigest()
        self._report(
            "load_back",
            source,
            operation.op_id,
            list(operation.source_pages),
            digests,
            prefix_indices=indices.cpu().tolist(),
            parts=[pool.kv_lora_rank, pool.qk_rope_head_dim],
        )

    def _submit_load_back(self, operation: _Read) -> None:
        operation.timing = RestoreTiming(self.profile)
        indices, pool = operation.indices, operation.pool
        main, index = self.manager.layout.prefix_main, self.manager.layout.prefix_index
        assert main is not None and index is not None and indices is not None
        count = len(operation.source_pages) * self.page_size
        if (
            indices.ndim != 1
            or indices.dtype != torch.int64
            or indices.numel() != count
            or indices.device != torch.device(self.device)
            or pool.page_size != self.page_size
            or pool.layer_num != main.layers
            or pool.kv_lora_rank + pool.qk_rope_head_dim != main.dim
            or len(pool.indexer_layer_ids) != index.layers
            or pool.index_head_dim != index.dim
            or pool.index_k_scale_buffer is not None
        ):
            raise ValueError("native prefix geometry differs from saved pages")
        pages = indices.cpu().reshape(-1, self.page_size)
        if (
            bool((pages < self.page_size).any())
            or pages[:, 0].unique().numel() != pages.shape[0]
            or bool((pages[:, 0] % self.page_size != 0).any())
            or not torch.equal(
                pages - pages[:, :1], torch.arange(self.page_size).expand_as(pages)
            )
        ):
            raise ValueError("native prefix requires complete unique allocator pages")
        assignments = assign_pages(
            list(range(len(operation.source_pages))), self.readers, self.read_assignment
        )
        rank = self.replica if self.readers > 1 else 0
        local_pages = assignments[rank]
        width = max(map(len, assignments))
        padded_count = width * self.page_size
        total_rows = self.readers * padded_count
        local_count = len(local_pages) * self.page_size
        offsets = torch.arange(self.page_size, dtype=torch.int64, device=self.device)
        source = torch.tensor(
            [operation.source_pages[p] for p in local_pages],
            dtype=torch.int64,
            device=self.device,
        )
        source = (source[:, None] * self.page_size + offsets).flatten()
        target = torch.arange(local_count, dtype=torch.int64, device=self.device)
        target += rank * padded_count
        valid = torch.ones_like(source, dtype=torch.bool)
        slot_pages: list[int | None] = [None] * (self.readers * width)
        slot_rows: list[int] = []
        native_rows: list[int] = []
        for reader, assigned in enumerate(assignments):
            for offset, page in enumerate(assigned):
                slot = reader * width + offset
                slot_pages[slot] = page
                slot_rows.extend(
                    range(slot * self.page_size, (slot + 1) * self.page_size)
                )
                native_rows.extend(pages[page].tolist())
        # One table per restore, reused by both data types and every layer.
        table = torch.tensor(
            [slot_rows, native_rows], dtype=torch.int64, device=self.device
        )
        buffers = operation.restore_buffers = {
            region: torch.empty(
                (total_rows, 1, layout.dim), dtype=torch.bfloat16, device=self.device
            )
            for region, layout in (("main", main), ("index", index))
        }
        buffer_bytes = {
            region: value.numel() * value.element_size()
            for region, value in buffers.items()
        }
        operation.retained.extend((source, target, valid, indices, table))
        if self.verify:
            # Acceptance metadata only; the oracle reuses the two KV buffers.
            oracle_pages = torch.tensor(
                operation.source_pages, dtype=torch.int64, device=self.device
            )
            oracle_source = (oracle_pages[:, None] * self.page_size + offsets).flatten()
            operation.oracle_indices = (
                oracle_source,
                torch.arange(count, dtype=torch.int64, device=self.device),
                torch.ones_like(oracle_source, dtype=torch.bool),
            )
        operation.split_read = dict(
            assignment=self.read_assignment,
            readers=self.readers,
            logical_pages=local_pages,
            padded_pages=width,
            slot_pages=slot_pages,
            slot_table_builds=1,
            slot_table_rows=table.shape[1],
            buffer_bytes=buffer_bytes,
            data_buffers=len(buffers),
            path=["H2D", "all_gather", "scatter"],
            launches={"H2D": 0, "all_gather": 0, "scatter": 0},
            read_bytes=0,
            oracle_read_bytes=0,
            private_replica_verified=False,
            gathered_verified=False,
            native_verified=False,
        )
        if not self._restore_logged:
            logger.info(
                "mempool split_read_buffers data=%s",
                json.dumps(
                    dict(
                        rank=self.replica,
                        rid=operation.rid,
                        slots=len(slot_pages),
                        page_size=self.page_size,
                        buffer_bytes=buffer_bytes,
                        data_buffers=2,
                    )
                ),
            )
            self._restore_logged = True
        if self._scatter_kernel is None:
            from .restore_kernel import scatter_rows

            self._scatter_kernel = scatter_rows
        for region, layout in (("main", main), ("index", index)):
            gathered = buffers[region]
            local = gathered[rank * padded_count : (rank + 1) * padded_count]
            for layer in range(layout.layers):
                view = self.manager.view(0, layer, region="prefix_" + region)
                with operation.timing.stage("H2D"):
                    if local_count:
                        self._copy(
                            self._dtype,
                            gathered,
                            source,
                            target,
                            valid,
                            layout.slots * layout.tokens,
                            total_rows,
                            layout.row_bytes,
                            local_count,
                            48,
                            view.device_base,
                            None,
                        )
                        operation.split_read["read_bytes"] += (
                            local_count * layout.row_bytes
                        )
                        operation.split_read["launches"]["H2D"] += 1
                with operation.timing.stage("all_gather"):
                    if self.readers > 1:
                        assert self.all_gather is not None
                        self.all_gather(gathered, local)
                        operation.split_read["launches"]["all_gather"] += 1
                if self.verify:
                    # Snapshot before scatter; its input is reused by the oracle.
                    actual = gathered.to(device="cpu", copy=True).reshape(
                        -1, self.page_size, 1, layout.dim
                    )
                if region == "main":
                    model_layer = pool.start_layer + layer
                    key, rope = (
                        pool.get_key_buffer(model_layer),
                        pool.get_value_buffer(model_layer),
                    )
                    key_dim, rope_dim = pool.kv_lora_rank, pool.qk_rope_head_dim
                    nz = pool.use_fia_nz
                else:
                    key, rope = (
                        pool.get_index_k_buffer(pool.indexer_layer_ids[layer]),
                        None,
                    )
                    key_dim, rope_dim, nz = pool.index_head_dim, 0, False
                for buffer, dim in ((key, key_dim), (rope, rope_dim)):
                    if buffer is not None and (
                        buffer.dtype != gathered.dtype
                        or int(pages.max()) >= buffer.numel() // dim
                    ):
                        raise ValueError(
                            "native prefix indices exceed the destination buffer"
                        )
                self._scatter_kernel(
                    gathered,
                    table,
                    key,
                    rope,
                    key_dim,
                    rope_dim,
                    self.page_size,
                    nz,
                    operation.timing,
                )
                operation.split_read["launches"]["scatter"] += 1
                if self.verify:
                    private = self._read_private_prefix(
                        operation, region, layer
                    ).reshape(-1, self.page_size, 1, layout.dim)
                    for slot, logical_page in enumerate(slot_pages):
                        if logical_page is not None and not torch.equal(
                            actual[slot], private[logical_page]
                        ):
                            raise RuntimeError(
                                "split_read differs from private prefix replica"
                            )
        operation.split_read["gathered_verified"] = self.verify
        operation.event = self._event_factory()
        operation.event.record()

    def _read_private_prefix(self, operation: _Read, region: str, layer: int) -> Tensor:
        """Read the actual replica via NPU into an idle restore buffer for V2."""
        assert operation.oracle_indices is not None
        source, target, valid = operation.oracle_indices
        buffer = operation.restore_buffers[region]
        view = self.manager.view(0, layer, region="prefix_" + region)
        count = source.numel()
        # At submission this follows scatter on the same stream. At receipt the
        # restore event has completed. CPU copying drains this diagnostic read.
        with self._read_context():
            self._copy(
                self._dtype,
                buffer,
                source,
                target,
                valid,
                view.layout.slots * view.layout.tokens,
                buffer.shape[0],
                view.layout.row_bytes,
                count,
                48,
                view.device_base,
                None,
            )
            result = buffer[:count].to(device="cpu", copy=True)
        operation.split_read["oracle_read_bytes"] += count * view.layout.row_bytes
        return result

    def _submit_fill(self, operation: _Read) -> None:
        offsets = torch.arange(self.page_size, dtype=torch.int64, device=self.device)
        pages = torch.tensor(
            operation.source_pages, dtype=torch.int64, device=self.device
        )
        source = (pages[:, None] * self.page_size + offsets).flatten()
        target = torch.arange(source.numel(), dtype=torch.int64, device=self.device)
        target += operation.slot * self.manager.layout.prompt.tokens
        valid = torch.ones_like(source, dtype=torch.bool)
        operation.retained.extend((source, target, valid))
        for region in ("main", "index"):
            _, layout = self.manager.layout.regions(0)["prefix_" + region]
            for layer in range(layout.layers):
                src = self.manager.view(0, layer, region="prefix_" + region)
                dst = self.manager.view(0, layer, region="alive_" + region)
                self._copy(
                    self._dtype,
                    self._dtype,
                    source,
                    target,
                    valid,
                    src.layout.slots * src.layout.tokens,
                    dst.layout.slots * dst.layout.tokens,
                    src.layout.row_bytes,
                    source.numel(),
                    48,
                    src.device_base,
                    dst.device_base,
                )
        operation.event = self._event_factory()
        operation.event.record()

    def _validate(self, operation: _Admit) -> None:
        positive_int("op_id", operation.op_id)
        check_index("source slot", operation.slot, self.manager.layout.prompt.slots)
        positive_int("prompt_tokens", operation.prompt_tokens)
        if (
            not operation.rid
            or operation.prompt_tokens % self.page_size
            or operation.prompt_tokens > self.manager.layout.prompt.tokens
        ):
            raise ValueError("invalid prefix source request")
        source, target = operation.source_pages, operation.target_pages
        if (
            not source
            or len(source) != len(target)
            or len(set(source)) != len(source)
            or len(set(target)) != len(target)
        ):
            raise ValueError("prefix requires unique paired source and target pages")
        for page in source:
            check_index("source page", page, operation.prompt_tokens // self.page_size)
        for page in target:
            if type(page) is not int or page not in self._free:
                raise ValueError("prefix target page is not free")

    def _copy(self, *args: Any) -> None:
        if self._kernel is None:
            importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
            self._kernel = torch.ops.npu.unidex_copy
        self._kernel(*args)

    def _submit(self, operation: _Admit) -> None:
        offsets = torch.arange(self.page_size, dtype=torch.int64, device=self.device)
        source = torch.tensor(
            operation.source_pages, dtype=torch.int64, device=self.device
        )
        target = torch.tensor(
            operation.target_pages, dtype=torch.int64, device=self.device
        )
        source = (source[:, None] * self.page_size + offsets).flatten()
        source += operation.slot * self.manager.layout.prompt.tokens
        target = (target[:, None] * self.page_size + offsets).flatten()
        valid = torch.ones_like(source, dtype=torch.bool)
        operation.retained.extend((source, target, valid))
        for region in ("main", "index"):
            _, destination_layout = self.manager.layout.regions(0)["prefix_" + region]
            for layer in range(destination_layout.layers):
                src = self.manager.view(0, layer, region="alive_" + region)
                dst = self.manager.view(0, layer, region="prefix_" + region)
                self._copy(
                    self._dtype,
                    self._dtype,
                    source,
                    target,
                    valid,
                    src.layout.slots * src.layout.tokens,
                    dst.layout.slots * dst.layout.tokens,
                    src.layout.row_bytes,
                    source.numel(),
                    48,
                    src.device_base,
                    dst.device_base,
                )
        operation.event = self._event_factory()
        operation.event.record()

    def assert_source_releasable(self, rid: str) -> None:
        operations: tuple[_Admit | _Read, ...] = (
            *self._operations.values(),
            *self._reads.values(),
        )
        if self._fault is not None or any(
            operation.rid == rid and operation.receipt is None
            for operation in operations
        ):
            raise RuntimeError("prefix source has pending or failed copies")

    def release_source(self, rid: str) -> None:
        """Discard per-request receipts after actual alive free, retaining prefix pages."""
        self.assert_source_releasable(rid)
        if self.verify:
            source = self._source
            if source is None or source.rid != rid:
                raise RuntimeError("prefix verification has no matching request")
            pages = self._check_pages("prefix", self._saved)
            self._report("retained", source, 0, [], pages)
            self._source = None
        self._operations = {
            op_id: operation
            for op_id, operation in self._operations.items()
            if operation.rid != rid
        }
        self._reads = {
            op_id: operation
            for op_id, operation in self._reads.items()
            if operation.rid != rid
        }

    def begin_request(self, rid: str, slot: int, tokens: int) -> None:
        if self.verify:
            if self._source is not None:
                raise RuntimeError("prefix verification request was not released")
            self._source = _Source(rid=rid, slot=slot, tokens=tokens)

    def capture_source(
        self,
        region: str,
        layer: int,
        values: Tensor,
        slots: Tensor,
        positions: Tensor,
        valid: Tensor,
    ) -> None:
        """Record actual computed values, never a copy read from the target region."""
        if not self.verify:
            return
        source = self._source
        if source is None:
            raise RuntimeError("prefix verification has no allocated source")
        mask = valid.cpu()
        rows, positions = slots.cpu()[mask], positions.cpu()[mask]
        if not bool(torch.all(rows == source.slot)) or bool(
            torch.any((positions < 0) | (positions >= source.tokens))
        ):
            raise RuntimeError("prefix reference differs from allocated source")
        key = region, layer
        if key not in source.values:
            source.values[key] = torch.empty(
                (source.tokens, *values.shape[1:]), dtype=values.dtype, device="cpu"
            )
            source.coverage[key] = torch.zeros(source.tokens, dtype=torch.bool)
        source.values[key][positions] = values.detach().cpu()[mask]
        source.coverage[key][positions] = True

    def _read_page(self, region: str, layer: int, page: int) -> Tensor:
        view = self.manager.view(0, layer, region=region)
        indices = torch.arange(self.page_size, device=self.device, dtype=torch.int64)
        result = torch.empty(
            (self.page_size, view.layout.heads, view.layout.dim),
            dtype=torch.bfloat16,
            device=self.device,
        )
        self._copy(
            self._dtype,
            result,
            indices + page * self.page_size,
            indices,
            torch.ones_like(indices, dtype=torch.bool),
            view.layout.slots * view.layout.tokens,
            self.page_size,
            view.layout.row_bytes,
            self.page_size,
            48,
            view.device_base,
            None,
        )
        return result.cpu()

    def _check_pages(
        self, pool: str, pages: dict[int, dict[tuple[str, int], Tensor]]
    ) -> dict[str, str]:
        digests = {}
        for page, reference in sorted(pages.items()):
            digest = hashlib.sha256()
            for (region, layer), expected in sorted(reference.items()):
                actual = self._read_page(pool + "_" + region, layer, page)
                if not torch.equal(actual, expected):
                    self._fault = f"prefix content mismatch pool={pool} page={page} region={region} layer={layer}"
                    raise RuntimeError(self._fault)
                digest.update(actual.contiguous().view(torch.uint8).numpy().tobytes())
            digests[str(page)] = digest.hexdigest()
        return digests

    def verify_alive(self, slot: int, tokens: int, *, start: int = 0) -> None:
        if not self.verify:
            return
        source = self._source
        if source is None or source.slot != slot:
            raise RuntimeError("prefix verification has no matching source slot")
        end = start + tokens
        if end != source.tokens or source.verified:
            return
        main, index = self.manager.layout.prefix_main, self.manager.layout.prefix_index
        assert main is not None and index is not None
        required = {
            (region, layer)
            for region, count in (("main", main.layers), ("index", index.layers))
            for layer in range(count)
        }
        if set(source.values) != required or any(
            not bool(mask[start:end].all()) for mask in source.coverage.values()
        ):
            raise RuntimeError("prefix reference is missing source layers/tokens")
        pages = {
            slot * (self.manager.layout.prompt.tokens // self.page_size) + page: {
                key: values[page * self.page_size : (page + 1) * self.page_size]
                for key, values in source.values.items()
            }
            for page in range(start // self.page_size, end // self.page_size)
        }
        digests = self._check_pages("alive", pages)
        # Report logical source pages; physical slot remains explicit.
        logical = {
            str(
                int(page) - slot * (self.manager.layout.prompt.tokens // self.page_size)
            ): digest
            for page, digest in digests.items()
        }
        source.verified = start == 0
        self._report(
            "suffix" if start else "alive",
            source,
            0,
            list(range(start // self.page_size, end // self.page_size)),
            logical,
        )

    def _verify_admit(self, operation: _Admit) -> None:
        source = self._source
        if (
            source is None
            or not source.verified
            or (source.rid, source.slot, source.tokens)
            != (operation.rid, operation.slot, operation.prompt_tokens)
        ):
            raise RuntimeError("prefix admit has no verified source")
        references = {
            target: {
                key: values[page * self.page_size : (page + 1) * self.page_size].clone()
                for key, values in source.values.items()
            }
            for page, target in zip(operation.source_pages, operation.target_pages)
        }
        pages = self._check_pages("prefix", references)
        self._saved.update(references)
        self._report(
            "admit", source, operation.op_id, list(operation.source_pages), pages
        )

    def _report(
        self,
        stage: str,
        source: _Source,
        op_id: int,
        source_pages: list[int],
        pages: dict[str, str],
        **evidence: Any,
    ) -> None:
        main, index = self.manager.layout.prefix_main, self.manager.layout.prefix_index
        assert main is not None and index is not None
        logger.info(
            "mempool prefix_content data=%s",
            json.dumps(
                dict(
                    stage=stage,
                    rank=self.replica,
                    rid=source.rid,
                    slot=source.slot,
                    op_id=op_id,
                    prompt_tokens=source.tokens,
                    source_pages=source_pages,
                    main_layers=main.layers,
                    index_layers=index.layers,
                    page_size=self.page_size,
                    pages=pages,
                    **evidence,
                ),
                sort_keys=True,
            ),
        )
