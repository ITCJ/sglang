"""Read NUMA memory blocks and report overlap with Ascend driver Fabric windows.

Uses only Python's standard library. No BM allocation, NPU initialization, or
system changes. Capacity is online memory-block address coverage, not free or
guaranteed allocatable memory. Windows come from the header's non-EMU_ST branch;
this does not query hardware registers or verify the loaded module's mapping.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
from pathlib import Path
from typing import Any, Iterable

GIB = 1 << 30
DEFAULT_HEADER = Path(
    "/usr/local/Ascend/driver/kernel/svmdrv/pmaster/common/inc/devmm_common.h"
)
AddressRange = tuple[int, int]


def c_integer(value: str) -> int:
    match = re.fullmatch(r"(0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*", value.strip())
    if match is None:
        raise ValueError(f"Expected a C integer literal, got {value!r}")
    literal = match[1]
    return int(literal, 16 if literal.lower().startswith("0x") else 10)


def read_windows(header: Path) -> list[AddressRange]:
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", header.read_text(), flags=re.S)
    # Select the production definition, never the adjacent 1 GiB EMU_ST value.
    name = "DEVMM_S2S_HOST_NODE_MEM_SIZE"
    text = re.sub(
        rf"#\s*ifdef\s+EMU_ST\s*\n\s*#\s*define\s+{name}[^\n]*\n"
        rf"\s*#\s*else\s*\n(\s*#\s*define\s+{name}[^\n]*\n)"
        r"\s*#\s*endif\b",
        r"\1",
        text,
    )

    def constant(name: str) -> int:
        values = re.findall(rf"^\s*#\s*define[ \t]+{name}[ \t]+([^\n]+)", text, re.M)
        if len(values) != 1:
            raise ValueError(f"Cannot select one non-EMU_ST {name} in {header}")
        value = c_integer(values[0])
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    size = constant(name)
    count = constant("DEVMM_S2S_HOST_NODE_NUM")
    array = re.search(
        r"\bdevmm_get_host_node_local_addr\s*\([^)]*\)\s*\{"
        r"\s*u64\s+mem_node_start\s*\[[^]]+\]\s*=\s*\{([^}]+)\}",
        text,
    )
    if array is None:
        raise ValueError(f"Cannot read mem_node_start in {header}")
    starts = [c_integer(item) for item in array[1].strip().rstrip(",").split(",")]
    if len(starts) != count:
        raise ValueError("mem_node_start length differs from DEVMM_S2S_HOST_NODE_NUM")
    return [(start, start + size) for start in starts]


def merge_ranges(ranges: Iterable[AddressRange]) -> list[AddressRange]:
    merged: list[AddressRange] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def range_bytes(ranges: Iterable[AddressRange]) -> int:
    return sum(end - start for start, end in ranges)


def inspect_overlap(sysfs_root: Path, header: Path) -> dict[str, Any]:
    windows = read_windows(header)
    block_size = int((sysfs_root / "memory/block_size_bytes").read_text().strip(), 16)
    if block_size <= 0:
        raise ValueError("Memory block size must be positive")
    node_dirs = sorted(
        (
            path
            for path in (sysfs_root / "node").glob("node[0-9]*")
            if re.fullmatch(r"node[0-9]+", path.name)
        ),
        key=lambda path: int(path.name[4:]),
    )
    if not node_dirs:
        raise ValueError(f"No NUMA nodes found under {sysfs_root / 'node'}")

    nodes = []
    owners: dict[int, int] = {}
    for node_dir in node_dirs:
        node_id = int(node_dir.name[4:])
        ranges = []
        offline_blocks = 0
        for block in node_dir.glob("memory[0-9]*"):
            if not re.fullmatch(r"memory[0-9]+", block.name):
                continue
            state = (block / "state").read_text().strip()
            if state == "offline":
                offline_blocks += 1
                continue
            if state != "online":
                raise ValueError(
                    f"{block}: state={state!r}; retry after memory hotplug"
                )
            index = int((block / "phys_index").read_text().strip(), 16)
            if index in owners:
                raise ValueError(
                    f"Memory block {index} belongs to node{owners[index]} and node{node_id}; "
                    "cannot determine per-node coverage at memory-block granularity"
                )
            owners[index] = node_id
            ranges.append((index * block_size, (index + 1) * block_size))
        ranges = merge_ranges(ranges)
        overlaps: list[dict[str, Any]] = []
        for window_id, (window_start, window_end) in enumerate(windows):
            intersections = [
                (max(start, window_start), min(end, window_end))
                for start, end in ranges
                if max(start, window_start) < min(end, window_end)
            ]
            if intersections:
                overlaps.append(dict(window_id=window_id, ranges=intersections))
        overlap_ranges = merge_ranges(
            interval for overlap in overlaps for interval in overlap["ranges"]
        )
        coverage = range_bytes(ranges)
        overlap_bytes = range_bytes(overlap_ranges)
        nodes.append(
            dict(
                node_id=node_id,
                ranges=ranges,
                coverage_bytes=coverage,
                overlap_bytes=overlap_bytes,
                outside_bytes=coverage - overlap_bytes,
                overlaps=overlaps,
                offline_blocks=offline_blocks,
            )
        )
    if not owners:
        raise ValueError(
            "No online memory blocks found; run with the host's full sysfs"
        )
    return dict(
        hostname=platform.node(),
        sysfs_root=str(sysfs_root),
        driver_header=str(header),
        window_branch="non-EMU_ST",
        measurement="online memory-block address coverage, not free memory",
        block_size_bytes=block_size,
        windows=[
            dict(window_id=i, start=start, end=end)
            for i, (start, end) in enumerate(windows)
        ],
        nodes=nodes,
        total_coverage_bytes=sum(node["coverage_bytes"] for node in nodes),
        total_overlap_bytes=sum(node["overlap_bytes"] for node in nodes),
    )


def print_report(report: dict[str, Any], details: bool) -> None:
    print(f"Host: {report['hostname']}")
    print(f"Driver header: {report['driver_header']} (non-EMU_ST)")
    print(f"Memory block size: {report['block_size_bytes'] / (1 << 20):g} MiB")
    print("Fabric windows from driver source (IDs are not Linux NUMA IDs):")
    for window in report["windows"]:
        start, end = window["start"], window["end"]
        print(
            f"  W{window['window_id']}: [{start:#x}, {end:#x}) {(end - start) / GIB:.2f} GiB"
        )
    print("\nNode  Coverage GiB  Overlap GiB  Outside GiB  Windows")
    for node in report["nodes"]:
        ids = ",".join(f"W{item['window_id']}" for item in node["overlaps"]) or "-"
        print(
            f"{node['node_id']:>4}  {node['coverage_bytes'] / GIB:>12.2f}"
            f"  {node['overlap_bytes'] / GIB:>11.2f}"
            f"  {node['outside_bytes'] / GIB:>11.2f}  {ids}"
        )
        if details:
            for start, end in node["ranges"]:
                print(f"      memory  [{start:#x}, {end:#x})")
            for overlap in node["overlaps"]:
                for start, end in overlap["ranges"]:
                    print(
                        f"      W{overlap['window_id']} hit  [{start:#x}, {end:#x}) {(end - start) / GIB:.2f} GiB"
                    )
        if node["offline_blocks"]:
            print(f"      skipped {node['offline_blocks']} offline blocks")
    print(
        f"Total coverage: {report['total_coverage_bytes'] / GIB:.2f} GiB; "
        f"overlap: {report['total_overlap_bytes'] / GIB:.2f} GiB"
    )
    print(
        "Coverage includes used/reserved areas and possible holes inside a block.\n"
        "Overlap is not MemFree or guaranteed BM capacity. No allocation was attempted.\n"
        "Header definitions do not verify the loaded driver or hardware window configuration."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver-header", type=Path, default=DEFAULT_HEADER)
    parser.add_argument(
        "--sysfs-root",
        type=Path,
        default=Path("/sys/devices/system"),
        help="System topology directory; can point to a saved sysfs fixture",
    )
    parser.add_argument(
        "--details", action="store_true", help="Print node and overlap ranges"
    )
    parser.add_argument(
        "--report", type=Path, help="Also write a JSON report (byte units)"
    )
    args = parser.parse_args()
    try:
        report = inspect_overlap(args.sysfs_root, args.driver_header)
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"NUMA/Fabric inspection failed: {exc}\n")
    print_report(report, args.details)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
