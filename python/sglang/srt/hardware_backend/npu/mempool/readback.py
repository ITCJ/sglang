"""Compare shadow BM selections on device and consume bounded completion evidence."""

from __future__ import annotations

from typing import Any

import msgspec
import torch
from torch import Tensor

from .copy import SparseCopyInputs, SparseKVCopy
from .manager import MempoolKVManager

# One record per layer/request row. Only this small snapshot crosses to the host.
_FIELDS = (
    "checks",
    "prompt_kv",
    "decode_kv",
    "invalid_kv",
    "mismatch_kv",
    "position",
    "feature",
    "topk",
    "prompt_boundary_kv",
    "decode_first_kv",
)


class KVReadbackError(RuntimeError):
    """Carry the request row so the service can identify its protocol attempt."""

    def __init__(self, message: str, row: int) -> None:
        """Keep device evidence in the message and request identity at the service seam."""
        super().__init__(message)
        self.row = row


class _Totals(msgspec.Struct):
    """Accumulate only validated forwards belonging to the current attachment."""

    forwards: int = 0
    replay_forwards: int = 0
    prompt_kv: int = 0
    decode_kv: int = 0
    prompt_boundary_kv: int = 0
    decode_first_kv: int = 0
    min_valid_per_layer: int | None = None
    min_topk: int | None = None
    max_topk: int = 0


class KVReadback:
    """Own diagnostic scratch/results; the runtime supplies lifetime and event ordering."""

    def __init__(
        self,
        manager: MempoolKVManager,
        req_pool_rows: int,
        start_layer: int,
        device: str,
        block_dim: int,
        kernel: Any = None,
    ) -> None:
        """Allocate small stable evidence tables before graph capture."""
        self.manager = manager
        self.start_layer = start_layer
        self.block_dim = block_dim
        self.kernel = kernel
        self.layers = manager.layout.decode.layers
        self.stats = torch.zeros(
            (self.layers, req_pool_rows, len(_FIELDS)), dtype=torch.int64, device=device
        )
        self._scratch: dict[tuple[int, int], tuple[SparseCopyInputs, Tensor]] = {}
        self._copies: dict[tuple[int, int, int], SparseKVCopy] = {}
        self._totals: dict[int, _Totals] = {}

    def bind(self, row: int) -> None:
        """Start fresh evidence for a newly approved attachment, including row reuse."""
        self._totals[row] = _Totals()

    def reset(self) -> None:
        """Reset per-forward evidence on the same stream that submits eager/replay work."""
        self.stats.zero_()

    def compare(
        self,
        layer: int,
        req_rows: Tensor,
        positions: Tensor,
        reference: Tensor,
        row_prompt_slot: Tensor,
        row_decode_slot: Tensor,
        row_prompt_len: Tensor,
        row_decode_len: Tensor,
    ) -> None:
        """Capture only device operations; reference must already be ready on this stream."""
        if positions.ndim != 2 or req_rows.numel() != positions.shape[0]:
            raise ValueError("readback requires one request row per top-k selection")
        batch, topk = positions.shape
        layout = self.manager.layout.decode
        if (
            tuple(reference.shape) != (batch, topk, layout.heads, layout.dim)
            or reference.dtype != torch.bfloat16
            or reference.device != self.stats.device
        ):
            raise ValueError("readback reference must match compact BF16 KV selection")
        key = (batch, topk)
        if key not in self._scratch:
            inputs = SparseCopyInputs(batch, topk, str(reference.device))
            self._scratch[key] = (inputs, torch.empty_like(reference))
        inputs, scratch = self._scratch[key]
        copy_key = (layer, batch, topk)
        if copy_key not in self._copies:
            self._copies[copy_key] = SparseKVCopy(
                self.manager.view(0, layer),
                self.manager.view(1, layer),
                inputs,
                block_dim=self.block_dim,
                kernel=self.kernel,
                output=scratch,
            )
        copier = self._copies[copy_key]
        row_valid = (req_rows > 0) & (req_rows < row_decode_slot.numel())
        safe_rows = torch.where(row_valid, req_rows, 0).long()
        active = row_valid & (row_decode_slot[safe_rows] >= 0)
        inputs.p_slots.copy_(row_prompt_slot[safe_rows])
        inputs.d_slots.copy_(row_decode_slot[safe_rows])
        inputs.prompt_lengths.copy_(row_prompt_len[safe_rows])
        inputs.decode_lengths.copy_(row_decode_len[safe_rows])
        inputs.positions.copy_(positions)
        inputs.valid.copy_(positions >= 0)
        inputs.active.copy_(active)
        actual = copier.gather()
        p_route, d_route = copier.routes()
        p_valid = p_route.valid.view(batch, topk)
        d_valid = d_route.valid.view(batch, topk)
        valid = p_valid | d_valid
        invalid = active[:, None] & (positions >= 0) & ~valid
        unequal = (actual != reference).flatten(2)
        mismatch = unequal.any(dim=2) & valid
        bad = invalid | mismatch
        first = bad.to(torch.int32).argmax(dim=1, keepdim=True)
        bad_position = positions.gather(1, first).squeeze(1)
        features = unequal.to(torch.int32).argmax(dim=2)
        bad_feature = features.gather(1, first).squeeze(1)
        has_bad = bad.any(dim=1)
        metrics = torch.stack(
            (
                active.long(),
                p_valid.sum(1),
                d_valid.sum(1),
                invalid.sum(1),
                mismatch.sum(1),
                torch.where(has_bad, bad_position, -1),
                torch.where(mismatch.gather(1, first).squeeze(1), bad_feature, -1),
                torch.full_like(safe_rows, topk),
                (p_valid & (positions == inputs.prompt_lengths[:, None] - 1)).sum(1),
                (d_valid & (positions == inputs.prompt_lengths[:, None])).sum(1),
            ),
            dim=1,
        )
        # Rows absent from the host plan and duplicate real rows are checked at
        # completion. Padding contributes nothing, even if it contains stale KV.
        metrics = torch.where(active[:, None], metrics, 0)
        self.stats[layer].index_add_(0, safe_rows, metrics)

    def complete(
        self, snapshot: Tensor, rows: tuple[int, ...], *, replay: bool
    ) -> None:
        """Validate an immutable snapshot only after its forward's event has completed."""
        values = snapshot.cpu().tolist()
        samples: dict[int, list[dict[str, int]]] = {row: [] for row in rows}
        for layer, layer_rows in enumerate(values):
            for row, raw in enumerate(layer_rows):
                item = dict(zip(_FIELDS, raw))
                if item["checks"] != int(row in samples):
                    raise KVReadbackError(
                        f"mempool KV readback coverage mismatch layer={layer + self.start_layer} "
                        f"row={row} checks={item['checks']} expected={int(row in samples)}",
                        row,
                    )
                if row not in samples:
                    continue
                if item["invalid_kv"] or item["mismatch_kv"]:
                    raise KVReadbackError(
                        f"mempool KV readback mismatch layer={layer + self.start_layer} "
                        f"row={row} position={item['position']} feature={item['feature']} "
                        f"invalid_kv={item['invalid_kv']} mismatch_kv={item['mismatch_kv']}",
                        row,
                    )
                samples[row].append(item)
        for row, checks in samples.items():
            totals = self._totals[row]
            totals.forwards += 1
            totals.replay_forwards += int(replay)
            for name in (
                "prompt_kv",
                "decode_kv",
                "prompt_boundary_kv",
                "decode_first_kv",
            ):
                setattr(
                    totals, name, getattr(totals, name) + sum(c[name] for c in checks)
                )
            valid_count = min(c["prompt_kv"] + c["decode_kv"] for c in checks)
            totals.min_valid_per_layer = (
                valid_count
                if totals.min_valid_per_layer is None
                else min(totals.min_valid_per_layer, valid_count)
            )
            width = min(c["topk"] for c in checks)
            totals.min_topk = (
                width if totals.min_topk is None else min(totals.min_topk, width)
            )
            totals.max_topk = max(totals.max_topk, max(c["topk"] for c in checks))

    def report(self, row: int | None) -> dict[str, Any]:
        """Return consumed evidence; a zero-decode request has no comparison claim."""
        totals = self._totals[row] if row is not None else _Totals()
        return {
            "status": "passed" if totals.forwards else "zero_decode",
            "layers": self.layers,
            "start_layer": self.start_layer,
            "layer_checks": self.layers * totals.forwards,
            **msgspec.structs.asdict(totals),
        }
