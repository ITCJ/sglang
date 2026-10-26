"""Retain prompt pages independently of alive slots and native request rows."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import logging
from typing import Any, Callable

import msgspec
import torch
from torch import Tensor

from .layout import check_index, positive_int
from .manager import MempoolKVManager

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
    ) -> None:
        positive_int("page_size", page_size)
        layout = manager.layout.prefix_main
        if manager.rank != 0 or layout is None or layout.tokens != page_size:
            raise ValueError("prefix store requires P's configured page regions")
        self.manager, self.page_size, self.device = manager, page_size, device
        self.verify = verify
        self.replica = replica
        self._kernel = kernel
        self._event_factory = event_factory or (lambda: torch.npu.Event())
        self._free = set(range(layout.slots))
        self._operations: dict[int, _Admit] = {}
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
        return copy.deepcopy(operation.receipt)

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
        if self._fault is not None or any(
            operation.rid == rid and operation.receipt is None
            for operation in self._operations.values()
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

    def verify_alive(self, slot: int, tokens: int) -> None:
        if not self.verify:
            return
        source = self._source
        if source is None or source.slot != slot:
            raise RuntimeError("prefix verification has no matching source slot")
        if tokens != source.tokens or source.verified:
            return
        main, index = self.manager.layout.prefix_main, self.manager.layout.prefix_index
        assert main is not None and index is not None
        required = {
            (region, layer)
            for region, count in (("main", main.layers), ("index", index.layers))
            for layer in range(count)
        }
        if set(source.values) != required or any(
            not bool(mask.all()) for mask in source.coverage.values()
        ):
            raise RuntimeError("prefix reference is missing source layers/tokens")
        pages = {
            slot * (self.manager.layout.prompt.tokens // self.page_size) + page: {
                key: values[page * self.page_size : (page + 1) * self.page_size]
                for key, values in source.values.items()
            }
            for page in range(tokens // self.page_size)
        }
        digests = self._check_pages("alive", pages)
        # Report logical source pages; physical slot remains explicit.
        logical = {
            str(
                int(page) - slot * (self.manager.layout.prompt.tokens // self.page_size)
            ): digest
            for page, digest in digests.items()
        }
        source.verified = True
        self._report("alive", source, 0, list(range(tokens // self.page_size)), logical)

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
                ),
                sort_keys=True,
            ),
        )
