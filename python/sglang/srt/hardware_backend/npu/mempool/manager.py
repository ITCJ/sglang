"""Own BM handles and expose logical KV views without exporting peer pointers."""

from __future__ import annotations

import importlib
import logging
import math
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import msgspec

from .diagnostics import startup_stage
from .layout import KVLayout, PoolLayout, check_index
from .sharing_plan import (
    NUMA_ENV,
    MempoolNumaConfig,
    PoolSession,
    contiguous_numa_node,
    parse_numa_nodes,
)

logger = logging.getLogger(__name__)
_NUMA_NODE_ENV = NUMA_ENV


def _configured_numa_nodes() -> tuple[int, ...]:
    """Validate the whole ordered node list before BM allocation, or use defaults."""
    value = os.environ.get(_NUMA_NODE_ENV)
    if value is None:
        return ()
    try:
        online = Path("/sys/devices/system/node/online").read_text().strip()
        return parse_numa_nodes(value, online)
    except (OSError, ValueError) as error:
        logger.warning(
            "Cannot apply %s=%r: %s; falling back to default BM allocation (flags=0)",
            _NUMA_NODE_ENV,
            value,
            error,
        )
        return ()


class BMHandle(Protocol):
    """Describe the MemFabric 1.1 BM operations used by one pool owner."""

    def join(self) -> int:
        """Join the peer pool and return the SDK status."""
        ...

    def leave(self) -> int:
        """Leave a joined pool after all NPU accesses have drained."""
        ...

    def destroy(self) -> None:
        """Destroy this pool handle."""
        ...

    def local_mem_size(self, mem_type: Any) -> int:
        """Report the actual local contribution for the specified memory type."""
        ...

    def peer_rank_ptr(self, rank: int, mem_type: Any) -> int:
        """Return a rank's global virtual address."""
        ...

    def gva_to_va(self, gva: int, mem_type: Any) -> int:
        """Translate a GVA to an address usable by the local NPU."""
        ...

    def copy_data(
        self, src: int, dst: int, size: int, copy_type: Any, flags: int = 0
    ) -> int:
        """Copy setup data using the SDK and return its status."""
        ...


class MempoolKVManager:
    """Own one mapped pool and its process-wide multi-member BM session."""

    _session_lock = threading.Lock()
    _session_active = False

    def __init__(
        self, layout: PoolLayout, rank: int, handle: BMHandle, bm_module: Any
    ) -> None:
        """Own an unjoined handle; direct create callers own MF/BM initialization."""
        layout.layout_for_rank(rank)
        self.layout = layout
        self.rank = rank
        self._handle = handle
        self._bm = bm_module
        self._bases: dict[int, tuple[int, int]] = {}
        self._joined = False
        self._closed = False
        self._owns_bm_context = False
        self._mapping_pending = "not checked"
        self.numa_node = -1
        self.session: PoolSession | None = None
        self.owners = (0, 1)
        self.contribution = layout.contribution_bytes(rank)

    @classmethod
    def create(
        cls,
        layout: PoolLayout,
        rank: int,
        pool_id: int = 0,
        bm_module: Any = None,
        *,
        tp_rank: int | None = None,
        tp_size: int = 16,
        numa: MempoolNumaConfig | None = None,
        host: str = "",
        session: PoolSession | None = None,
    ) -> MempoolKVManager:
        """Allocate DRAM using contiguous NUMA blocks for this host's TP ranks.

        The current rank-pair service has all 16 ranks on each host. Component
        callers with a different local rank count must supply tp_size explicitly.
        """
        numa_nodes: tuple[int, ...]
        if session is not None:
            numa_node = session.numa_node
            numa_nodes = ()
        else:
            numa = numa or MempoolNumaConfig.from_environment()
            numa.require_rank_pair_startup()
            numa.validate_local_map(host, tp_size)
            layout.layout_for_rank(rank)
            numa_nodes = () if numa.rank_map is not None else _configured_numa_nodes()
            numa_node = -1
            if numa.rank_map is not None:
                numa_node = next(
                    a.numa_node for a in numa.rank_map if a.rank == tp_rank
                )
            elif numa_nodes:
                if type(tp_rank) is not int or tp_rank < 0:
                    raise ValueError(
                        f"{_NUMA_NODE_ENV} requires a nonnegative integer tp_rank"
                    )
                numa_node = contiguous_numa_node(numa_nodes, tp_rank, tp_size)
        # MF's performance bit enables the NUMA ID in the low seven bits.
        bm_flags = 0 if numa_node == -1 else (1 << 7) | numa_node
        if bm_module is None:
            bm_module = importlib.import_module("memfabric_hybrid").bm
        local_bytes = (
            session.contribution if session else layout.contribution_bytes(rank)
        )
        logger.info(
            "Creating mempool BM pool: bm_rank=%d pool_id=%d "
            "local_dram_bytes=%d rank_stride_bytes=%d "
            "tp_rank=%s local_numa_nodes=%s numa_node=%d bm_flags=%d",
            rank,
            pool_id,
            local_bytes,
            layout.rank_stride_bytes,
            tp_rank,
            ",".join(map(str, numa_nodes)) or "default",
            numa_node,
            bm_flags,
        )
        with startup_stage(
            "bm.create2",
            resources=True,
            bm_rank=rank,
            pool_id=pool_id,
            local_dram_bytes=local_bytes,
            max_dram_bytes=layout.rank_stride_bytes,
            local_hbm_bytes=0,
            max_hbm_bytes=0,
            data_op="SDMA",
            tp_rank=tp_rank,
            local_numa_nodes=numa_nodes,
            numa_node=numa_node,
            bm_flags=bm_flags,
        ):
            handle = bm_module.create2(
                id=pool_id,
                local_dram_size=local_bytes,
                max_dram_size=layout.rank_stride_bytes,
                local_hbm_size=0,
                max_hbm_size=0,
                data_op_type=bm_module.BmDataOpType.SDMA,
                flags=bm_flags,
            )
            if handle is None:
                raise RuntimeError("BM create2 returned no handle")
        logger.info("Mempool BM pool created: bm_rank=%d pool_id=%d", rank, pool_id)
        manager = cls(layout, rank, handle, bm_module)
        manager.numa_node = numa_node
        manager.session = session
        if session is not None:
            manager.owners = session.owners
            manager.contribution = session.contribution
        return manager

    @classmethod
    def initialize_session(
        cls,
        *,
        session: PoolSession,
        store_host: str,
        nic_host: str,
        device_id: int,
        timeout: float = 120.0,
        bm_module: Any = None,
    ) -> MempoolKVManager:
        """Initialize, allocate and map a planned session; retain MF for TransferEngine."""
        rank = session.participant
        role = "prefill" if session.side == 0 else "decode"
        tp_rank = session.tp_rank
        nic_address = f"[{nic_host}]" if ":" in nic_host else nic_host
        store_address = f"[{store_host}]" if ":" in store_host else store_host
        nic_url = f"tcp://{nic_address}:{session.nic_base_port}"
        if bm_module is None:
            bm_module = importlib.import_module("memfabric_hybrid").bm
        with cls._session_lock:
            if cls._session_active:
                raise RuntimeError("This worker already owns a BM session")
            config = bm_module.BmConfig()
            config.auto_ranking = False
            config.rank_id = rank
            config.start_store = rank == 0
            config.init_timeout = config.create_timeout = config.operation_timeout = (
                math.ceil(timeout)
            )
            config.set_nic(nic_url)
            store_port = session.store_port
            store_url = f"tcp://{store_address}:{store_port}"
            if rank != 0:
                # MF 1.1's initial TCP connect retries ignore config.init_timeout.
                # P may still be loading weights when D reaches this point.
                with startup_stage("bm.wait_store", tp_rank=tp_rank, store=store_url):
                    cls._wait_for_store(store_host, store_port, timeout)
            logger.info(
                "Initializing mempool BM session: role=%s tp_rank=%d pid=%d "
                "device_id=%d store=%s nic=%s",
                role,
                tp_rank,
                os.getpid(),
                device_id,
                store_url,
                nic_url,
            )
            with startup_stage(
                "bm.initialize",
                role=role,
                tp_rank=tp_rank,
                bm_rank=rank,
                device_id=device_id,
                store=store_url,
                nic=nic_url,
                world_size=session.world_size,
                auto_ranking=False,
                start_store=rank == 0,
                sdk_timeout=math.ceil(timeout),
            ):
                ret = bm_module.initialize(
                    store_url, session.world_size, device_id, config
                )
                logger.info(
                    "Mempool BM initialize returned: role=%s tp_rank=%d ret=%d",
                    role,
                    tp_rank,
                    ret,
                )
                if ret != 0:
                    raise RuntimeError(f"BM initialize failed for {store_url}: {ret}")

            manager = None
            try:
                if bm_module.bm_rank_id() != rank:
                    raise RuntimeError("BM initialized with an unexpected rank ID")
                manager = cls.create(
                    session.layout,
                    session.side,
                    session.pool_id,
                    bm_module,
                    tp_rank=tp_rank,
                    session=session,
                )
                manager.join(timeout)
            except Exception:
                try:
                    if manager is not None:
                        manager.close(drain=lambda: None)
                finally:
                    with startup_stage("bm.uninitialize_after_failure", bm_rank=rank):
                        bm_module.uninitialize()
                raise
            manager._owns_bm_context = True
            cls._session_active = True
            return manager

    @staticmethod
    def _wait_for_store(store_host: str, port: int, timeout: float) -> None:
        """Wait for P's TCP listener; BM still performs its own peer handshake."""
        store_url = f"tcp://{store_host}:{port}"
        started = time.monotonic()
        deadline = started + timeout
        next_log = started + 30
        last_error: OSError | None = None
        logger.info("Waiting for P BM store %s (timeout=%gs)", store_url, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"P BM store {store_url} was not reachable within {timeout:g}s"
                ) from last_error
            try:
                # Send no MF header or rank identity. MF 1.1 closes this probe
                # at header validation, before registering a BM peer.
                with socket.create_connection(
                    (store_host, port), timeout=min(1.0, remaining)
                ):
                    pass
            except OSError as error:
                last_error = error
                now = time.monotonic()
                if now >= next_log:
                    logger.info(
                        "Still waiting for P BM store %s (%.1fs remaining): %s",
                        store_url,
                        max(0, deadline - now),
                        error,
                    )
                    next_log = now + 30
                time.sleep(min(0.2, max(0, deadline - now)))
            else:
                logger.info(
                    "P BM store %s is reachable after %.1fs",
                    store_url,
                    time.monotonic() - started,
                )
                return

    def join(self, timeout: float = 120.0) -> None:
        """Join and publish addresses only after both ranks' mappings are valid."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("mapping timeout must be finite and positive")
        if self._closed:
            raise RuntimeError("mempool is closed")
        if self._joined:
            raise RuntimeError("mempool has already joined")
        logger.info("Joining mempool BM pool: bm_rank=%d", self.rank)
        with startup_stage("bm.join", bm_rank=self.rank):
            ret = self._handle.join()
            if ret != 0:
                raise RuntimeError(f"BM join failed: {ret}")
        self._joined = True
        with startup_stage("bm.inspect_pool", bm_rank=self.rank):
            actual_bytes = self._handle.local_mem_size(self._bm.BmMemType.HOST)
            if actual_bytes != self.contribution:
                raise RuntimeError(f"BM local contribution mismatch: {actual_bytes}")
            logger.info(
                "Mempool BM join returned: bm_rank=%d local_dram_bytes=%d p_gva=%#x d_gva=%#x; "
                "checking device mappings (timeout=%gs)",
                self.rank,
                actual_bytes,
                self._handle.peer_rank_ptr(0, self._bm.BmMemType.HOST),
                self._handle.peer_rank_ptr(1, self._bm.BmMemType.HOST),
                timeout,
            )
        with startup_stage("bm.wait_mappings", bm_rank=self.rank, timeout=timeout):
            self._wait_for_mappings(timeout)

    def _wait_for_mappings(self, timeout: float) -> None:
        """Publish bases only when both ranks have contiguous device mappings."""
        started = time.monotonic()
        deadline = started + timeout
        next_log = started
        while True:
            bases = self._mapped_bases()
            if bases is not None:
                self._bases = bases
                logger.info(
                    "Mempool BM mappings ready: bm_rank=%d elapsed=%.1fs "
                    "p_device_va=%#x d_device_va=%#x",
                    self.rank,
                    time.monotonic() - started,
                    bases[0][1],
                    bases[1][1],
                )
                return
            now = time.monotonic()
            if now >= next_log:
                logger.info(
                    "[MEMPOOL_INIT] MAPPING_PENDING bm_rank=%d pid=%d elapsed=%.1fs %s",
                    self.rank,
                    os.getpid(),
                    now - started,
                    self._mapping_pending,
                )
                next_log = now + 30
            if now >= deadline:
                raise TimeoutError(
                    "BM peer mappings did not become device-visible: "
                    + self._mapping_pending
                )
            # MF logs each unavailable GVA at ERROR; poll once per second
            # while the peer finishes allocation/import, capped by the deadline.
            time.sleep(min(1.0, max(0, deadline - time.monotonic())))

    def _mapped_bases(self) -> dict[int, tuple[int, int]] | None:
        """Check stride and device-contiguous layer ranges, or retry incomplete maps."""
        first = self._handle.peer_rank_ptr(0, self._bm.BmMemType.HOST)
        gvas = [
            self._handle.peer_rank_ptr(owner, self._bm.BmMemType.HOST)
            for owner in self.owners
        ]
        if not first or not all(gvas):
            self._mapping_pending = f"owner_gvas={gvas}"
            return None
        if any(
            gva - first != owner * self.layout.rank_stride_bytes
            for owner, gva in zip(self.owners, gvas)
        ):
            raise RuntimeError("BM GVA rank stride differs from the common maximum")
        bases = {}
        for rank, gva in enumerate(gvas):
            device_va = self._handle.gva_to_va(gva, self._bm.BmMemType.LOCAL_DEVICE)
            if not device_va:
                self._mapping_pending = f"missing_rank={rank} gva={gva:#x} offset=0"
                return None
            offsets = {self.layout.contribution_bytes(rank) - 1}
            for base, layout in self.layout.regions(rank).values():
                for layer in range(layout.layers):
                    offsets.add(base + layer * layout.layer_bytes)
                    offsets.add(base + (layer + 1) * layout.layer_bytes - 1)
            for offset in offsets:
                mapped = self._handle.gva_to_va(
                    gva + offset, self._bm.BmMemType.LOCAL_DEVICE
                )
                if not mapped:
                    self._mapping_pending = (
                        f"missing_rank={rank} gva={gva + offset:#x} offset={offset}"
                    )
                    return None
                if mapped != device_va + offset:
                    raise RuntimeError("BM layer mapping is not device-contiguous")
            bases[rank] = gva, device_va
        return bases

    def replica_registration(self) -> dict[str, Any]:
        """Describe each local replica with the session's physical owner and members."""
        session = self.session
        assert session is not None
        return {
            name: dict(
                replica=session.members[0],
                owner=session.members[0],
                members=list(session.members),
                numa_node=self.numa_node,
                session=session.identity,
                participant=session.participant,
                owner_participant=session.owners[self.rank],
                world_size=session.world_size,
                store_port=session.store_port,
                nic_port=session.nic_base_port + session.participant,
                bytes=geometry.total_bytes,
            )
            for name, (_, geometry) in self.layout.regions(self.rank).items()
        }

    def placement_registration(self, tp_rank: int, host: str) -> dict[str, Any]:
        """Describe this owner's replicas and query only its local BM contribution."""
        from .placement import sample_pool_placement

        gva, device = self.bases(self.rank)
        host_base = self._handle.gva_to_va(gva, self._bm.BmMemType.LOCAL_HOST)
        regions = {
            name: (offset, layout.total_bytes)
            for name, (offset, layout) in self.layout.regions(self.rank).items()
        }
        evidence = sample_pool_placement(
            host_base,
            self._handle.local_mem_size(self._bm.BmMemType.HOST),
            self.numa_node,
            regions,
        )
        evidence["gpu_registered_range"] = dict(
            address=hex(device),
            bytes=self.layout.contribution_bytes(self.rank),
            registration_seconds=None,
            timing_note="SDK exposes no separate GPU registration timer",
        )
        return dict(
            host=host,
            owner_tp_rank=tp_rank,
            numa_placement_verified=evidence["placement_verified"],
            numa_evidence=evidence,
        )

    def view(
        self, rank: int, layer: int, *, region: str = "alive_main"
    ) -> MempoolKVView:
        """Return a typed layer view that keeps its owning pool alive."""
        offset, layout = self.layout.regions(rank)[region]
        check_index("layer", layer, layout.layers)
        return MempoolKVView(self, rank, layer, layout, offset)

    def local_cpu_view(self, layer: int, *, region: str) -> Any:
        """Borrow the local DRAM layer for acceptance; the manager owns its lifetime."""
        import ctypes

        import torch

        view = self.view(self.rank, layer, region=region)
        address = self._handle.gva_to_va(view.gva_base, self._bm.BmMemType.LOCAL_HOST)
        storage = (ctypes.c_ubyte * view.layout.layer_bytes).from_address(address)
        return torch.frombuffer(storage, dtype=torch.bfloat16).view(view.shape)

    def bases(self, rank: int) -> tuple[int, int]:
        """Return this process's verified GVA and NPU base for a pool rank."""
        if self._closed:
            raise RuntimeError("mempool is closed")
        if not self._joined or rank not in self._bases:
            raise RuntimeError("mempool mappings are not ready")
        return self._bases[rank]

    def probe_peer(self, marker: Any) -> None:
        """Write a fixed-size marker to the peer's reserved startup probe range."""
        peer = 1 - self.rank
        gva = self.bases(peer)[0] + self.layout.probe_offset(peer)
        if marker.numel() * marker.element_size() != self.layout.probe_bytes:
            raise ValueError("mapping probe size differs from the reserved range")
        self._copy_tensor_to_gva(marker, gva)

    def verify_local_probe(self, expected: bytes, device: str) -> None:
        """Read the actual BM marker to prove the ZMQ peer is this pool's peer."""
        import torch

        if len(expected) != self.layout.probe_bytes:
            raise ValueError("pool identity probe must occupy exactly 64 bytes")
        marker = torch.empty(len(expected), dtype=torch.uint8, device=device)
        gva = self.bases(self.rank)[0] + self.layout.probe_offset(self.rank)
        ret = self._handle.copy_data(
            gva, marker.data_ptr(), len(expected), self._bm.BmCopyType.G2L, 0
        )
        if ret != 0:
            raise RuntimeError(f"BM identity probe read failed: {ret}")
        torch.npu.synchronize()
        if bytes(marker.cpu().tolist()) != expected:
            raise RuntimeError("BM peer marker differs from the ZMQ peer session")

    def _copy_tensor_to_gva(self, values: Any, gva: int) -> None:
        """Synchronously write setup data; runtime graph writes use UniDexCopy."""
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
        """Drain before destroying the pool and any BM context started here."""
        if self._closed:
            return
        with startup_stage("bm.close_drain", bm_rank=self.rank):
            drain()
        if self._joined:
            with startup_stage("bm.leave", bm_rank=self.rank):
                ret = self._handle.leave()
                if ret != 0:
                    raise RuntimeError(f"BM leave failed: {ret}")
        with startup_stage("bm.destroy", bm_rank=self.rank):
            self._handle.destroy()
        self._closed = True
        if self._owns_bm_context:
            with type(self)._session_lock:
                with startup_stage("bm.uninitialize", bm_rank=self.rank):
                    self._bm.uninitialize()
                type(self)._session_active = False


class MempoolKVView(msgspec.Struct, frozen=True):
    """Expose typed logical coordinates over one rank's mapped KV layer."""

    owner: MempoolKVManager
    rank: int
    layer: int
    layout: KVLayout
    region_offset: int = 0

    @property
    def shape(self) -> tuple[int, int, int, int]:
        """Return [slots, tokens, heads, dim] without allocating a backing tensor."""
        return self.layout.shape

    @property
    def dtype(self) -> str:
        """Return the element type of the logical tensor."""
        return self.layout.dtype

    @property
    def gva_base(self) -> int:
        """Return the layer's GVA for SDK operations."""
        return (
            self.owner.bases(self.rank)[0]
            + self.region_offset
            + self.layer * self.layout.layer_bytes
        )

    @property
    def device_base(self) -> int:
        """Return the stable layer address used by this process's NPU kernels."""
        return (
            self.owner.bases(self.rank)[1]
            + self.region_offset
            + self.layer * self.layout.layer_bytes
        )

    def element_gva(self, slot: int, token: int, head: int = 0, column: int = 0) -> int:
        """Resolve a checked logical element to its SDK GVA."""
        offset = self.layout.byte_offset(self.layer, slot, token, head, column)
        return self.owner.bases(self.rank)[0] + self.region_offset + offset

    def element_device_ptr(
        self, slot: int, token: int, head: int = 0, column: int = 0
    ) -> int:
        """Resolve a checked logical element to its local NPU address."""
        offset = self.layout.byte_offset(self.layer, slot, token, head, column)
        return self.owner.bases(self.rank)[1] + self.region_offset + offset

    def write_rows(self, slot: int, token: int, values: Any) -> None:
        """Synchronously stage contiguous local KV rows outside Graph capture."""
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
