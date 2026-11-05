"""Stable graph outputs for selected-page counts; collected after completion."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

import torch
from torch import Tensor


class PageObservation:
    """Count selected, hit, cache miss, P IO and D IO rows independently."""

    def __init__(
        self,
        layers: int,
        rows: int,
        context: int,
        page_size: int,
        topk: int,
        producers: tuple[int, ...],
        start_layer: int,
        device: str,
    ) -> None:
        self.page_size = page_size
        self.page_count = (context + page_size - 1) // page_size
        self.start_layer, self.producers = start_layer, producers
        self.counts = torch.zeros(
            (layers, rows, self.page_count, 6), dtype=torch.int64, device=device
        )
        self.sources = torch.zeros((layers, rows, 12), dtype=torch.int64, device=device)
        self.cache_changes = torch.zeros(
            (layers, rows, 3), dtype=torch.int64, device=device
        )
        self.refill_check = torch.zeros(
            (layers, rows, 2), dtype=torch.int64, device=device
        )
        self.map_check = torch.zeros(
            (layers, rows, 3), dtype=torch.int64, device=device
        )
        self.equal = torch.zeros((layers, rows), dtype=torch.int64, device=device)
        self.previous = torch.full((rows, topk), -1, dtype=torch.int64, device=device)

    def reset(self) -> None:
        self.refill_check.zero_()
        self.map_check.zero_()
        self.sources.zero_()
        self.cache_changes.zero_()
        self.counts.zero_()
        self.equal.zero_()
        self.previous.fill_(-1)

    def _add(
        self, layer: int, rows: Tensor, positions: Tensor, values: Tensor, column: int
    ) -> None:
        page = (
            positions.clamp(min=0, max=self.page_count * self.page_size - 1)
            // self.page_size
        )
        indices = rows.long()[:, None] * self.page_count + page.long()
        self.counts[layer, :, :, column].view(-1).scatter_add_(
            0, indices.reshape(-1), values.long().reshape(-1)
        )

    def cache(
        self,
        layer_id: int,
        rows: Tensor,
        positions: Tensor,
        valid: Tensor,
        hits: Tensor,
    ) -> None:
        layer = layer_id - self.start_layer
        valid = valid & (rows[:, None] > 0)
        self._add(layer, rows, positions, valid, 0)
        self._add(layer, rows, positions, hits & valid, 1)
        self._add(layer, rows, positions, ~hits & valid, 2)
        selected = torch.where(valid, positions.long(), -1)
        if layer_id in self.producers:
            self.previous.index_copy_(0, rows.long(), selected)
        matches = (selected == self.previous[rows.long()]).all(1).long()
        self.equal[layer].index_copy_(0, rows.long(), matches)

    def io(
        self,
        layer_id: int,
        rows: Tensor,
        positions: Tensor,
        misses: Tensor,
        prompt_lengths: Tensor,
    ) -> None:
        layer = layer_id - self.start_layer
        valid = misses & (rows[:, None] > 0) & (positions >= 0)
        prompt = prompt_lengths[rows.long(), None]
        self._add(layer, rows, positions, valid & (positions < prompt), 3)
        self._add(layer, rows, positions, valid & (positions >= prompt), 4)
        # An unaligned prompt can split one logical selected page across two
        # physical D pages. Retain the earlier-page count for exact locations.
        self._add(
            layer,
            rows,
            positions,
            valid
            & (positions >= prompt)
            & (positions % self.page_size < prompt % self.page_size),
            5,
        )

    def materialized(
        self, layer_id: int, rows: Tensor, regions: Tensor, valid: Tensor
    ) -> None:
        layer = layer_id - self.start_layer
        keys = (
            rows.long()[:, None].expand(-1, regions.numel() // rows.numel()).flatten()
            * 12
            + regions
        )
        self.sources[layer].view(-1).scatter_add_(0, keys, valid.long())

    def cache_updates(
        self, layer_id: int, rows: Tensor, previous: Tensor, hits: Tensor, valid: Tensor
    ) -> None:
        layer = layer_id - self.start_layer
        changes = torch.stack(
            ((previous - hits.sum(1)).clamp(min=0), valid.sum(1), valid.sum(1)), dim=1
        )
        self.cache_changes[layer].index_add_(0, rows.long(), changes)

    def refill_verified(
        self,
        layer_id: int,
        rows: Tensor,
        valid: Tensor,
        actual: Tensor,
        selected: Tensor,
    ) -> None:
        errors = (actual != selected).flatten(2).any(2) & valid
        self.refill_check[layer_id - self.start_layer].index_add_(
            0, rows.long(), torch.stack((valid.sum(1), errors.sum(1)), dim=1)
        )

    def map_verified(
        self,
        layer_id: int,
        rows: Tensor,
        valid: Tensor,
        found: Tensor,
        positions: Tensor,
        expected: Tensor,
        present: Tensor,
    ) -> None:
        missing = valid & (~found | (positions != expected))
        stale = (present - valid.sum(1)).clamp(min=0)
        self.map_check[layer_id - self.start_layer].index_add_(
            0, rows.long(), torch.stack((valid.sum(1), missing.sum(1), stale), dim=1)
        )

    def materialization_snapshot(self, rows: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        checks = torch.cat((self.refill_check, self.map_check), dim=2)
        return (
            self.sources.index_select(1, rows),
            self.cache_changes.index_select(1, rows),
            checks.index_select(1, rows),
        )

    def snapshot(self, rows: Tensor | None = None) -> tuple[Tensor, Tensor]:
        if rows is None:
            return self.counts.clone(), self.equal.clone()
        return self.counts.index_select(1, rows), self.equal.index_select(1, rows)


@contextmanager
def device_range(name: str) -> Iterator[None]:
    handle = torch.npu.mstx.range_start("mempool:" + name, torch.npu.current_stream())
    try:
        yield
    finally:
        torch.npu.mstx.range_end(handle)


class DeviceTrace:
    """Keep profiling active from capture to an explicit idle-stage export."""

    def __init__(self, root: Path) -> None:
        torch_npu = importlib.import_module("torch_npu")

        self.root = root
        self.profile = torch_npu.profiler.profile(
            activities=[
                torch_npu.profiler.ProfilerActivity.CPU,
                torch_npu.profiler.ProfilerActivity.NPU,
            ],
            record_shapes=False,
            with_stack=False,
            experimental_config=torch_npu.profiler._ExperimentalConfig(
                profiler_level=torch_npu.profiler.ProfilerLevel.Level1,
                mstx=True,
                export_type=[torch_npu.profiler.ExportType.Text],
            ),
            on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(str(root)),
        )
        self.profile.start()
        self.stopped = False

    def stop(self) -> None:
        if not self.stopped:
            self.profile.stop()
            self.stopped = True

    def install_hooks(self, model: Any) -> None:
        """Hooks enqueue captured device ranges; replay executes their tasks."""
        for name, module in model.named_modules():
            leaf = name.rsplit(".", 1)[-1]
            if hasattr(module, "layer_communicator"):
                category = "layer"
            elif leaf in ("indexer", "mlp", "self_attn"):
                category = {
                    "indexer": "indexer",
                    "mlp": "moe",
                    "self_attn": "attention",
                }[leaf]
            else:
                continue
            module.register_forward_pre_hook(self._pre(f"{category}:{name}"))
            module.register_forward_hook(self._post)

    def install_collectives(self, groups: list[Any]) -> None:
        """Instrument existing group instances; preserve their calls and ordering."""
        seen = set()
        for group in groups:
            if id(group) in seen or group.world_size == 1:
                continue
            seen.add(id(group))
            for name in (
                "all_reduce",
                "all_gather_into_tensor",
                "all_gather",
                "reduce_scatter_tensor",
                "reduce_scatter",
                "all_gatherv",
                "reduce_scatterv",
            ):
                original = getattr(group, name)
                setattr(group, name, self._collective(original, name))

    @staticmethod
    def _collective(original: Any, name: str) -> Any:
        @wraps(original)
        def call(*args: Any, **kwargs: Any) -> Any:
            # CPU control collectives retain their host-only meaning.
            if args and isinstance(args[0], Tensor) and args[0].device.type == "npu":
                with device_range("collective:" + name):
                    return original(*args, **kwargs)
            return original(*args, **kwargs)

        return call

    @staticmethod
    def _pre(name: str) -> Any:
        def begin(module: Any, inputs: Any) -> None:
            module._mempool_observation_range = torch.npu.mstx.range_start(
                "mempool:" + name, torch.npu.current_stream()
            )

        return begin

    @staticmethod
    def _post(module: Any, inputs: Any, output: Any) -> None:
        torch.npu.mstx.range_end(module._mempool_observation_range)
