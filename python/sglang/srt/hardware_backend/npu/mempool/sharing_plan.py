"""Plan NUMA assignments and shared storage without importing an NPU runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .layout import PoolLayout, positive_int

SHARE_ENV = "SGLANG_NPU_MEMPOOL_SHARE"
NUMA_ENV = "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE"


def parse_numa_nodes(value: str, online: str) -> tuple[int, ...]:
    """Validate the complete legacy list while preserving its rotation order."""
    parts = [part.strip() for part in value.split(",")]
    if any(not part.isascii() or not part.isdecimal() for part in parts):
        raise ValueError("expected a comma-separated list of NUMA node IDs")
    nodes = tuple(int(part) for part in parts)
    if any(node > 126 for node in nodes):
        raise ValueError("NUMA node IDs must be in [0, 126]")
    if len(nodes) != len(set(nodes)):
        raise ValueError("NUMA node IDs must not repeat")
    online_nodes: set[int] = set()
    for part in online.strip().split(","):
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
        if match is None:
            raise ValueError(f"invalid local NUMA topology: {online!r}")
        first, last = int(match[1]), int(match[2] or match[1])
        if first > last:
            raise ValueError(f"invalid local NUMA topology: {online!r}")
        # Only the SDK-addressable nodes matter; do not expand arbitrary ranges.
        online_nodes.update(range(first, min(last, 126) + 1))
    missing = [node for node in nodes if node not in online_nodes]
    if missing:
        raise ValueError(
            f"nodes {missing} are not online on this host (online nodes: {online})"
        )
    return nodes


@dataclass(frozen=True)
class HostTopology:
    """Describe one physical host as reported by the launch topology."""

    host: str
    ranks: tuple[int, ...]
    online_nodes: str | None


@dataclass(frozen=True)
class RankNuma:
    """Assign one global TP rank to a requested node on its physical host."""

    host: str
    rank: int
    numa_node: int

    def validate(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("NUMA assignment requires a physical host identity")
        if type(self.rank) is not int or self.rank < 0:
            raise ValueError(f"invalid global TP rank: {self.rank!r}")
        if type(self.numa_node) is not int or not 0 <= self.numa_node <= 126:
            raise ValueError(f"rank {self.rank}: NUMA node must be in [0, 126]")


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"duplicate rank_numa_map field: {key}")
        record[key] = value
    return record


@dataclass(frozen=True)
class SharingGroup:
    """Identify ranks sharing one complete logical P or D replica."""

    host: str
    numa_node: int
    members: tuple[int, ...]
    nic_offset: int = 0

    @property
    def owner_tp_rank(self) -> int:
        return self.members[0]


@dataclass(frozen=True)
class SharingPlan:
    """Describe storage intent; no allocation or physical placement is implied."""

    role: str
    instance_id: str
    kv_identity: str
    layout: PoolLayout
    mempool_share: bool
    assignments: tuple[RankNuma, ...]
    groups: tuple[SharingGroup, ...]
    warnings: tuple[str, ...] = ()
    base_port: int = 19000
    nic_port: int = 25670
    pool_id: int = 104

    def validate_peer(self, peer: SharingPlan) -> None:
        """Reject incompatible P/D plans before allocating either side's pool."""
        if self.role == peer.role:
            raise ValueError("mempool plans require opposite P/D roles")
        for name in (
            "instance_id",
            "kv_identity",
            "mempool_share",
            "pool_id",
            "base_port",
        ):
            if getattr(self, name) != getattr(peer, name):
                raise ValueError(f"mempool peer {name} differs")
        if self.layout != peer.layout:
            raise ValueError("mempool peer KV layout differs")
        local_members = tuple(group.members for group in self.groups)
        peer_members = tuple(group.members for group in peer.groups)
        if local_members != peer_members:
            raise ValueError(
                f"mempool peer group members differ: local={local_members} peer={peer_members}"
            )
        endpoints = {
            (group.host, self._nic_port(group, member))
            for group in self.groups
            for member in range(len(group.members))
        }
        collisions = endpoints.intersection(
            (group.host, peer._nic_port(group, member))
            for group in peer.groups
            for member in range(len(group.members))
        )
        if collisions:
            raise ValueError(
                f"mempool peer NIC endpoints collide: {sorted(collisions)}"
            )

    def _nic_port(self, group: SharingGroup, member: int) -> int:
        """Apply MF's participant offset to each member's reserved NIC range."""
        size = len(group.members)
        participant = (0 if self.role == "prefill" else size) + member
        return self.nic_port + group.nic_offset + 2 * size * member + participant

    def group_layout(self, group: SharingGroup) -> PoolLayout:
        """Reserve one session probe per member without changing token capacity."""
        return PoolLayout(
            self.layout.prompt,
            self.layout.decode,
            self.layout.alignment_bytes,
            max(self.layout.probe_bytes, 64 * len(group.members)),
        )

    def group_identity(self, group: SharingGroup) -> str:
        """Identify a paired logical pool independently of P/D host or NUMA IDs."""
        value = {
            "version": 1,
            "instance_id": self.instance_id,
            "kv_identity": self.kv_identity,
            "tp_size": len(self.assignments),
            "mempool_share": self.mempool_share,
            "members": group.members,
            "pool_id": self.pool_id,
            "layout": self.group_layout(group).signature(),
            "writer_rule": "contiguous-rows-v1",
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def report(self) -> dict[str, Any]:
        """Expose planned contributions and mapping identities, never measured RAM."""
        side = 0 if self.role == "prefill" else 1
        rows: list[dict[str, Any]] = []
        for group in self.groups:
            size = len(group.members)
            layout = self.group_layout(group)
            identity = self.group_identity(group)
            for member, rank in enumerate(group.members):
                participant = side * size + member
                nic_port = self._nic_port(group, member)
                rows.append(
                    {
                        "rank": rank,
                        "host": group.host,
                        "numa_node": group.numa_node,
                        "group": group.members,
                        "owner_tp_rank": group.owner_tp_rank,
                        "participant": participant,
                        "world_size": 2 * size,
                        "owner_participant": side * size,
                        "contribution_bytes": layout.contribution_bytes(side)
                        if member == 0
                        else 0,
                        "rank_stride_bytes": layout.rank_stride_bytes,
                        "probe_offset": layout.probe_offset(side) + 64 * member,
                        "store_port": self.base_port + group.owner_tp_rank,
                        "nic_base_port": nic_port - participant,
                        "nic_port": nic_port,
                        "pool_id": self.pool_id,
                        "pool_identity": identity,
                        "placement_verified": False,
                    }
                )
        rows.sort(key=lambda row: row["rank"])
        return {
            "allocation_status": "planned",
            "role": self.role,
            "mempool_share": self.mempool_share,
            "group_count": len(self.groups),
            "planned_contribution_bytes": sum(
                row["contribution_bytes"] for row in rows
            ),
            "ranks": rows,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class MempoolNumaConfig:
    """Freeze the sharing switch independently of NUMA placement."""

    mempool_share: bool = False
    legacy_nodes: str | None = None
    rank_map: tuple[RankNuma, ...] | None = None

    def require_rank_pair_startup(self) -> None:
        """Keep unimplemented group placement out of the legacy BM allocator."""
        if self.mempool_share or self.rank_map is not None:
            raise ValueError(
                "mempool sharing/rank_numa_map requires group startup (ticket02-2); "
                "this build supports planning only for these options"
            )

    @classmethod
    def from_environment(
        cls,
        *,
        rank_numa_map: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> MempoolNumaConfig:
        """Read the switch strictly; only explicit 1 enables sharing."""
        values = os.environ if environ is None else environ
        share = values.get(SHARE_ENV, "0")
        if share not in ("0", "1"):
            raise ValueError(f"{SHARE_ENV} must be 0 or 1, got {share!r}")
        rank_map = None
        if rank_numa_map is not None:
            if NUMA_ENV in values:
                raise ValueError(f"rank_numa_map conflicts with {NUMA_ENV}")
            try:
                records = json.loads(
                    Path(rank_numa_map).read_text(), object_pairs_hook=_unique_fields
                )
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"cannot read rank_numa_map {rank_numa_map!r}: {error}"
                ) from error
            if not isinstance(records, list) or not records:
                raise ValueError(
                    "rank_numa_map must be a nonempty array of host/rank/node records"
                )
            assignments = []
            for record in records:
                if not isinstance(record, dict) or set(record) != {
                    "host",
                    "rank",
                    "node",
                }:
                    raise ValueError(
                        "rank_numa_map records require exactly host, rank and node"
                    )
                assignment = RankNuma(record["host"], record["rank"], record["node"])
                assignment.validate()
                assignments.append(assignment)
            rank_map = tuple(assignments)
        return cls(share == "1", values.get(NUMA_ENV), rank_map)

    def plan(
        self,
        *,
        hosts: Sequence[HostTopology],
        tp_size: int,
        role: str,
        instance_id: str,
        kv_identity: str,
        layout: PoolLayout,
        base_port: int = 19000,
        nic_port: int = 25670,
        pool_id: int = 104,
    ) -> SharingPlan:
        """Resolve NUMA first, then apply the sharing switch to that assignment."""
        positive_int("tp_size", tp_size)
        if role not in ("prefill", "decode"):
            raise ValueError("role must be prefill or decode")
        for name, value in (("instance_id", instance_id), ("kv_identity", kv_identity)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty identity")
        if type(pool_id) is not int or not 0 <= pool_id < 256:
            raise ValueError("BM pool_id must be in [0, 256)")
        for name, port in (("base_port", base_port), ("nic_port", nic_port)):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError(f"invalid {name}: {port!r}")
        if type(self.mempool_share) is not bool:
            raise ValueError("mempool_share must be a bool")
        if self.rank_map is not None and self.legacy_nodes is not None:
            raise ValueError(f"rank_numa_map conflicts with {NUMA_ENV}")
        rank_hosts: dict[int, HostTopology] = {}
        host_ids: set[str] = set()
        for host in hosts:
            if (
                not isinstance(host.host, str)
                or not host.host.strip()
                or host.host in host_ids
            ):
                raise ValueError(f"invalid or duplicate physical host: {host.host!r}")
            if not host.ranks:
                raise ValueError(f"host {host.host!r} has no TP ranks")
            host_ids.add(host.host)
            for rank in host.ranks:
                if (
                    type(rank) is not int
                    or not 0 <= rank < tp_size
                    or rank in rank_hosts
                ):
                    raise ValueError(
                        f"duplicate or invalid TP rank {rank!r} on host {host.host!r}"
                    )
                rank_hosts[rank] = host
        if len(rank_hosts) != tp_size:
            raise ValueError(
                f"topology missing TP ranks: {sorted(set(range(tp_size)) - rank_hosts.keys())}"
            )
        assignments: list[RankNuma] = []
        warnings = []
        if self.rank_map is not None:
            seen: set[int] = set()
            for assignment in self.rank_map:
                assignment.validate()
                if assignment.rank in seen or assignment.rank not in rank_hosts:
                    raise ValueError(
                        f"duplicate or invalid mapped TP rank {assignment.rank}"
                    )
                host = rank_hosts[assignment.rank]
                if host.host != assignment.host:
                    raise ValueError(
                        f"rank {assignment.rank}: mapped host {assignment.host!r} differs from topology {host.host!r}"
                    )
                parse_numa_nodes(str(assignment.numa_node), host.online_nodes or "")
                seen.add(assignment.rank)
            if len(seen) != tp_size:
                raise ValueError(
                    f"rank_numa_map missing TP ranks: {sorted(rank_hosts.keys() - seen)}"
                )
            assignments.extend(self.rank_map)
        for host in hosts:
            if self.rank_map is not None:
                continue
            nodes: tuple[int, ...] = ()
            if self.legacy_nodes is not None:
                try:
                    nodes = parse_numa_nodes(self.legacy_nodes, host.online_nodes or "")
                except ValueError as error:
                    if self.mempool_share:
                        raise ValueError(
                            f"host {host.host!r}: {NUMA_ENV}: {error}"
                        ) from error
                    warnings.append(
                        f"host {host.host!r}: Cannot apply {NUMA_ENV}={self.legacy_nodes!r}: "
                        f"{error}; falling back to default BM allocation (flags=0)"
                    )
            if self.mempool_share and not nodes:
                raise ValueError(
                    f"host {host.host!r}: sharing requires an explicit NUMA assignment"
                )
            assignments.extend(
                RankNuma(host.host, rank, nodes[rank % len(nodes)] if nodes else -1)
                for rank in host.ranks
            )
        assignments.sort(key=lambda assignment: assignment.rank)
        members: dict[tuple[str, int, int], list[int]] = {}
        for assignment in assignments:
            key = (
                assignment.host,
                assignment.numa_node,
                -1 if self.mempool_share else assignment.rank,
            )
            members.setdefault(key, []).append(assignment.rank)
        groups = []
        nic_offset = 0
        for (host_id, node, _), ranks in members.items():
            groups.append(SharingGroup(host_id, node, tuple(ranks), nic_offset))
            # MF adds participant rank to the supplied NIC base. Give each
            # member a disjoint 2g range, as in the validated component setup.
            nic_offset += 2 * len(ranks) ** 2
        if base_port + tp_size - 1 > 65535 or nic_port + nic_offset - 1 > 65535:
            raise ValueError("mempool store/NIC port range exceeds 65535")
        if max(base_port, nic_port) <= min(
            base_port + tp_size - 1, nic_port + nic_offset - 1
        ):
            raise ValueError("mempool store and NIC port ranges overlap")
        return SharingPlan(
            role,
            instance_id,
            kv_identity,
            layout,
            self.mempool_share,
            tuple(assignments),
            tuple(groups),
            tuple(warnings),
            base_port,
            nic_port,
            pool_id,
        )
