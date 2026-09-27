"""Own BM handles and expose logical KV views without exporting peer pointers."""

from __future__ import annotations

import importlib
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .layout import KVLayout, PoolLayout, check_index


class BMHandle(Protocol):
    def join(self) -> int: ...
    def leave(self) -> int: ...
    def destroy(self) -> None: ...
    def local_mem_size(self, mem_type: Any) -> int: ...
    def peer_rank_ptr(self, rank: int, mem_type: Any) -> int: ...
    def gva_to_va(self, gva: int, mem_type: Any) -> int: ...
    def copy_data(
        self, src: int, dst: int, size: int, copy_type: Any, flags: int = 0
    ) -> int: ...


class MempoolKVManager:
    """BM must be initialized by the caller; this manager owns one joined pool."""

    def __init__(
        self, layout: PoolLayout, rank: int, handle: BMHandle, bm_module: Any
    ) -> None:
        self.layout = layout
        self.rank = rank
        self._handle = handle
        self._bm = bm_module
        self._bases: dict[int, tuple[int, int]] = {}
        self._joined = False
        self._closed = False

    @classmethod
    def create(
        cls,
        layout: PoolLayout,
        rank: int,
        pool_id: int = 0,
        bm_module: Any = None,
    ) -> MempoolKVManager:
        layout.layout_for_rank(rank)
        if bm_module is None:
            bm_module = importlib.import_module("memfabric_hybrid").bm
        handle = bm_module.create2(
            id=pool_id,
            local_dram_size=layout.contribution_bytes(rank),
            max_dram_size=layout.rank_stride_bytes,
            local_hbm_size=0,
            max_hbm_size=0,
            data_op_type=bm_module.BmDataOpType.SDMA,
        )
        if handle is None:
            raise RuntimeError("BM create2 returned no handle")
        return cls(layout, rank, handle, bm_module)

    def join(self, timeout: float = 120.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("mapping timeout must be finite and positive")
        if self._closed:
            raise RuntimeError("mempool is closed")
        if self._joined:
            raise RuntimeError("mempool has already joined")
        ret = self._handle.join()
        if ret != 0:
            raise RuntimeError(f"BM join failed: {ret}")
        self._joined = True
        actual_bytes = self._handle.local_mem_size(self._bm.BmMemType.HOST)
        if actual_bytes != self.layout.contribution_bytes(self.rank):
            raise RuntimeError(f"BM local contribution mismatch: {actual_bytes}")
        deadline = time.monotonic() + timeout
        while True:
            bases = self._mapped_bases()
            if bases is not None:
                self._bases = bases
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("BM peer mappings did not become device-visible")
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _mapped_bases(self) -> dict[int, tuple[int, int]] | None:
        gvas = [
            self._handle.peer_rank_ptr(rank, self._bm.BmMemType.HOST) for rank in (0, 1)
        ]
        if not all(gvas):
            return None
        if gvas[1] - gvas[0] != self.layout.rank_stride_bytes:
            raise RuntimeError("BM GVA rank stride differs from the common maximum")
        bases = {}
        for rank, gva in enumerate(gvas):
            device_va = self._handle.gva_to_va(gva, self._bm.BmMemType.LOCAL_DEVICE)
            if not device_va:
                return None
            layout = self.layout.layout_for_rank(rank)
            offsets = {self.layout.contribution_bytes(rank) - 1}
            for layer in range(layout.layers):
                offsets.add(layer * layout.layer_bytes)
                offsets.add((layer + 1) * layout.layer_bytes - 1)
            for offset in offsets:
                mapped = self._handle.gva_to_va(
                    gva + offset, self._bm.BmMemType.LOCAL_DEVICE
                )
                if not mapped:
                    return None
                if mapped != device_va + offset:
                    raise RuntimeError("BM layer mapping is not device-contiguous")
            bases[rank] = gva, device_va
        return bases

    def view(self, rank: int, layer: int) -> MempoolKVView:
        layout = self.layout.layout_for_rank(rank)
        check_index("layer", layer, layout.layers)
        return MempoolKVView(self, rank, layer, layout)

    def bases(self, rank: int) -> tuple[int, int]:
        if self._closed:
            raise RuntimeError("mempool is closed")
        if not self._joined or rank not in self._bases:
            raise RuntimeError("mempool mappings are not ready")
        return self._bases[rank]

    def probe_peer(self, marker: Any) -> None:
        peer = 1 - self.rank
        gva = self.bases(peer)[0] + self.layout.probe_offset(peer)
        if marker.numel() * marker.element_size() != self.layout.probe_bytes:
            raise ValueError("mapping probe size differs from the reserved range")
        self._copy_tensor_to_gva(marker, gva)

    def _copy_tensor_to_gva(self, values: Any, gva: int) -> None:
        import torch

        if values.device.type != "npu" or not values.is_contiguous():
            raise ValueError("BM writes require a contiguous NPU source")
        # Setup writes use synchronous BM copy; complete the producer first.
        torch.npu.synchronize()
        ret = self._handle.copy_data(
            values.data_ptr(),
            gva,
            values.numel() * values.element_size(),
            self._bm.BmCopyType.L2G,
            0,
        )
        if ret != 0:
            raise RuntimeError(f"BM write to {gva:#x} failed: {ret}")

    def close(self, drain: Callable[[], None]) -> None:
        if self._closed:
            return
        drain()
        if self._joined:
            ret = self._handle.leave()
            if ret != 0:
                raise RuntimeError(f"BM leave failed: {ret}")
        self._handle.destroy()
        self._closed = True


@dataclass(frozen=True)
class MempoolKVView:
    owner: MempoolKVManager
    rank: int
    layer: int
    layout: KVLayout

    @property
    def shape(self) -> tuple[int, int, int, int]:
        return self.layout.shape

    @property
    def dtype(self) -> str:
        return self.layout.dtype

    @property
    def gva_base(self) -> int:
        return self.owner.bases(self.rank)[0] + self.layer * self.layout.layer_bytes

    @property
    def device_base(self) -> int:
        return self.owner.bases(self.rank)[1] + self.layer * self.layout.layer_bytes

    def element_gva(self, slot: int, token: int, head: int = 0, column: int = 0) -> int:
        offset = self.layout.byte_offset(self.layer, slot, token, head, column)
        return self.owner.bases(self.rank)[0] + offset

    def element_device_ptr(
        self, slot: int, token: int, head: int = 0, column: int = 0
    ) -> int:
        offset = self.layout.byte_offset(self.layer, slot, token, head, column)
        return self.owner.bases(self.rank)[1] + offset

    def write_rows(self, slot: int, token: int, values: Any) -> None:
        import torch

        if self.rank != self.owner.rank:
            raise ValueError("KV writes must target the owning rank's view")
        if values.dtype != torch.bfloat16:
            raise ValueError("KV writes require bfloat16")
        if values.ndim != 3 or tuple(values.shape[1:]) != (
            self.layout.heads,
            self.layout.dim,
        ):
            raise ValueError("KV write source must have shape [tokens, heads, dim]")
        count = values.shape[0]
        if count <= 0 or token + count > self.layout.tokens:
            raise ValueError("KV write exceeds the view's token capacity")
        self.owner._copy_tensor_to_gva(values, self.element_gva(slot, token))
