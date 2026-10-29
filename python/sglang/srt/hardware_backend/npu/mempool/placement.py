"""Query owned BM host pages without touching or migrating the KV contents."""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def query_page_nodes(addresses: list[int]) -> list[int]:
    """Ask Linux move_pages for residency only (nodes=NULL, flags=0)."""
    numa = ctypes.CDLL("libnuma.so.1", use_errno=True)
    query = numa.move_pages
    query.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]
    query.restype = ctypes.c_int
    pages = (ctypes.c_void_p * len(addresses))(*addresses)
    status = (ctypes.c_int * len(addresses))()
    if query(0, len(addresses), pages, None, status, 0) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return list(status)


def sample_pool_placement(
    host_base: int,
    size: int,
    node: int,
    regions: dict[str, tuple[int, int]],
    *,
    proc: Path = Path("/proc/self"),
    query_pages: Callable[[list[int]], list[int]] = query_page_nodes,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Sample each KV region and account for resident bytes in owned VMAs.

    Driver mappings can be opaque to both Linux interfaces. Preserve that as
    missing evidence, never substitute allocation flags for physical placement.
    """
    page_size = page_size or os.sysconf("SC_PAGE_SIZE")
    evidence: dict[str, Any] = dict(
        host_address=hex(host_base),
        allocation_bytes=size,
        requested_numa_node=node,
        regions={},
        mappings=[],
        resident_bytes={},
        placement_verified=False,
    )
    addresses: set[int] = set()
    # Include the allocation padding as well as each logical region.
    for name, (offset, length) in {**regions, "allocation": (0, size)}.items():
        first = (host_base + offset) // page_size * page_size
        last = (host_base + offset + length - 1) // page_size * page_size
        count = (last - first) // page_size
        samples = sorted({first + (count * i // 16) * page_size for i in range(17)})
        addresses.update(samples)
        evidence["regions"][name] = dict(
            offset=offset, bytes=length, sample_addresses=samples
        )
    ordered = sorted(addresses)
    try:
        statuses = dict(zip(ordered, query_pages(ordered)))
        for region in evidence["regions"].values():
            region["nodes"] = [
                statuses[address] for address in region["sample_addresses"]
            ]
        evidence["samples_verified"] = bool(statuses) and all(
            n == node for n in statuses.values()
        )
    except (OSError, AttributeError) as error:
        evidence.update(samples_verified=False, query_error=str(error))
    try:
        numa_rows = {
            int(row.split()[0], 16): row
            for row in (proc / "numa_maps").read_text().splitlines()
        }
        resident: dict[int, int] = {}
        covered = 0
        for line in (proc / "maps").read_text().splitlines():
            start, end = (int(v, 16) for v in line.split()[0].split("-"))
            if end <= host_base or start >= host_base + size:
                continue
            row = numa_rows.get(start, "")
            evidence["mappings"].append(dict(mapping=line, numa_map=row))
            # A larger VMA may include other allocations; its counts aren't ours.
            if start < host_base or end > host_base + size:
                continue
            page_kb = re.search(r"\bkernelpagesize_kB=(\d+)", row)
            if page_kb is None:
                continue
            covered += end - start
            for n, pages in re.findall(r"\bN(\d+)=(\d+)", row):
                resident[int(n)] = (
                    resident.get(int(n), 0) + int(pages) * int(page_kb[1]) * 1024
                )
        evidence["resident_bytes"] = resident
        evidence["resident_verified"] = covered == size and resident == {node: size}
    except OSError as error:
        evidence.update(resident_verified=False, resident_error=str(error))
    evidence["placement_verified"] = (
        evidence["samples_verified"] and evidence["resident_verified"]
    )
    evidence["status"] = "verified" if evidence["placement_verified"] else "unverified"
    return evidence
