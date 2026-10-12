"""Test-only four-participant allocation using the production logical KV views."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .sharing import SharingPlan

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.manager import MempoolKVManager
else:
    from .manager import MempoolKVManager


def cpu_list(value: str) -> set[int]:
    result: set[int] = set()
    for part in value.strip().split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if match is None:
            raise ValueError(f"invalid kernel CPU/node list: {value!r}")
        first, last = int(match[1]), int(match[2] or match[1])
        if last < first:
            raise ValueError("reversed CPU/node range")
        result.update(range(first, last + 1))
    return result


def numa_evidence(address: int, node: int) -> dict[str, Any]:
    """Attribute the owner's host mapping to resident pages, not reserved VA."""
    maps = Path("/proc/self/maps").read_text().splitlines()
    rows = Path("/proc/self/numa_maps").read_text().splitlines()
    for mapping in maps:
        lo, hi = (int(v, 16) for v in mapping.split()[0].split("-"))
        if lo <= address < hi:
            row = next((r for r in rows if int(r.split()[0], 16) == lo), "")
            pages = {int(n): int(p) for n, p in re.findall(r"\bN(\d+)=(\d+)", row)}
            return dict(
                host_address=hex(address),
                mapping=mapping,
                numa_map=row,
                resident_pages=pages,
                placement_verified=pages.get(node, 0) > 0
                and all(n == node or p == 0 for n, p in pages.items()),
            )
    return dict(
        host_address=hex(address),
        placement_verified=False,
        reason="owner host mapping absent from /proc/self/maps",
    )


class SharingPool(MempoolKVManager):
    """Keep BM participant identity separate from the logical P/D view rank.

    This adapter is deliberately confined to the experiment. It does not change
    the serving manager's two-participant or ownership contract.
    """

    def __init__(
        self,
        plan: SharingPlan,
        mode: str,
        side: int,
        member: int,
        bm: Any,
        *,
        numa_node: int,
        pool_id: int = 110,
    ) -> None:
        if numa_node not in cpu_list(
            Path("/sys/devices/system/node/online").read_text()
        ):
            raise ValueError(f"NUMA node {numa_node} is not online")
        if not 0 <= numa_node <= 126:
            raise ValueError("NUMA node must be in [0, 126]")
        self.plan, self.mode, self.member = plan, mode, member
        self.participant = side * 2 + member if mode == "shared" else side
        self.world_size = len(plan.participants(mode))
        self.contribution = plan.contributions(mode)[side * 2 + member]
        self.events: list[dict[str, Any]] = []
        layout = plan.layout()
        handle = bm.create2(
            id=pool_id,
            local_dram_size=self.contribution,
            max_dram_size=layout.rank_stride_bytes,
            local_hbm_size=0,
            max_hbm_size=0,
            data_op_type=bm.BmDataOpType.SDMA,
            flags=0x80 | numa_node,
        )
        if handle is None:
            raise RuntimeError("BM create2 returned no handle")
        super().__init__(layout, side, handle, bm)
        self._handle: Any = handle
        self.numa_node = numa_node

    def join(self, timeout: float = 120.0) -> None:
        if self._closed or self._joined:
            raise RuntimeError("pool is closed or already joined")
        if not hasattr(self._handle, "set_group_event_handler"):
            raise RuntimeError("SDK lacks BM group event reporting")

        def record(rank: int, event: Any) -> None:
            self.events.append(
                dict(participant=rank, event=str(event), ns=time.monotonic_ns())
            )

        # Retain the callable for the entire handle lifetime.
        self._group_callback = record
        self.check(
            self._handle.set_group_event_handler(record), "set_group_event_handler"
        )
        self.check(self._handle.join(), "join")
        self._joined = True
        actual = self._handle.local_mem_size(self._bm.BmMemType.HOST)
        if actual != self.contribution:
            raise RuntimeError(f"physical contribution {actual} != {self.contribution}")
        self._wait_for_mappings(timeout)

    @staticmethod
    def check(result: Any, operation: str) -> None:
        if result not in (None, 0):
            raise RuntimeError(f"BM {operation} failed: {result}")

    def _mapped_bases(self) -> dict[int, tuple[int, int]] | None:
        bases = {}
        first_gva = self._handle.peer_rank_ptr(0, self._bm.BmMemType.HOST)
        for side in (0, 1):
            owner = self.plan.owner(self.mode, side=side, member=self.member)
            gva = self._handle.peer_rank_ptr(owner, self._bm.BmMemType.HOST)
            va = (
                self._handle.gva_to_va(gva, self._bm.BmMemType.LOCAL_DEVICE)
                if gva
                else 0
            )
            if not first_gva or not va:
                self._mapping_pending = f"owner={owner} gva={gva:#x} va={va:#x}"
                return None
            if gva - first_gva != owner * self.layout.rank_stride_bytes:
                raise RuntimeError("owner GVA differs from participant stride")
            layout = self.layout.layout_for_rank(side)
            offsets = {self.layout.contribution_bytes(side) - 1}
            for layer in range(layout.layers):
                offsets.update(
                    (layer * layout.layer_bytes, (layer + 1) * layout.layer_bytes - 1)
                )
            for offset in offsets:
                mapped = self._handle.gva_to_va(
                    gva + offset, self._bm.BmMemType.LOCAL_DEVICE
                )
                if not mapped:
                    self._mapping_pending = f"owner={owner} offset={offset}"
                    return None
                if mapped != va + offset:
                    raise RuntimeError("owner device mapping is not contiguous")
            bases[side] = (gva, va)
        return bases

    def rejoin_follower(self, timeout: float) -> None:
        """Caller first drains ALL participants; exercise SDK group rebuilding."""
        if self.mode != "shared" or self.member != 1:
            raise ValueError("only a zero-contribution follower can rejoin")
        before = dict(self._bases)
        self.check(self._handle.leave(), "follower leave")
        self._joined = False
        self.check(self._handle.join(), "follower rejoin")
        self._joined = True
        self._wait_for_mappings(timeout)
        if self._bases != before:
            raise RuntimeError("owner addresses changed after follower rejoin")

    def wait_membership(self, follower: int, timeout: float, *, since: int) -> None:
        """Observers must see both SDK notifications, rather than infer reachability."""
        if self.participant == follower:
            return
        deadline = time.monotonic() + timeout
        while True:
            events = [
                r["event"] for r in self.events[since:] if r["participant"] == follower
            ]
            left = False
            for event in events:
                if event.endswith("LEAVE_EVENT"):
                    left = True
                elif left and event.endswith("JOIN_EVENT"):
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"SDK membership events missing for follower {follower}: {events}"
                )
            time.sleep(0.05)

    def probe(self, torch: Any, global_rank: int) -> None:
        marker = torch.full((64,), global_rank + 1, dtype=torch.uint8, device="npu")
        for side in (0, 1):
            gva = (
                self.bases(side)[0]
                + self.layout.probe_offset(side)
                + self.participant * 64
            )
            self._copy_tensor_to_gva(marker, gva)

    def verify_probe(self, torch: Any) -> None:
        for side in (0, 1):
            for participant in range(self.world_size):
                global_rank = (
                    participant
                    if self.mode == "shared"
                    else participant * 2 + self.member
                )
                marker = torch.empty(64, dtype=torch.uint8, device="npu")
                self.check(
                    self._handle.copy_data(
                        self.bases(side)[0]
                        + self.layout.probe_offset(side)
                        + participant * 64,
                        marker.data_ptr(),
                        64,
                        self._bm.BmCopyType.G2L,
                        0,
                    ),
                    "probe read",
                )
                torch.npu.synchronize()
                if marker.cpu().tolist() != [global_rank + 1] * 64:
                    raise AssertionError("participant identity probe mismatch")

    def report(self) -> dict[str, Any]:
        evidence: dict[str, Any] = dict(
            placement_verified=True, resident_pages={}, zero_contribution=True
        )
        if self.contribution:
            host = self._handle.gva_to_va(
                self.bases(self.rank)[0], self._bm.BmMemType.LOCAL_HOST
            )
            evidence = numa_evidence(host, self.numa_node)
        return dict(
            participant=self.participant,
            logical_side=self.rank,
            member=self.member,
            local_dram_bytes=self._handle.local_mem_size(self._bm.BmMemType.HOST),
            payload_bytes=self.layout.layout_for_rank(self.rank).total_bytes
            if self.contribution
            else 0,
            probe_bytes=self.layout.probe_bytes if self.contribution else 0,
            rank_stride_bytes=self.layout.rank_stride_bytes,
            reserved_dram_va_bytes=self.world_size * self.layout.rank_stride_bytes,
            requested_numa_node=self.numa_node,
            numa=evidence,
            owners={
                side: dict(gva=hex(gva), device_va=hex(va))
                for side, (gva, va) in self._bases.items()
            },
            group_events=list(self.events),
            reach_status_method="group events, direct owner access, drained follower leave/rejoin; no BM reach query in Python SDK",
        )
