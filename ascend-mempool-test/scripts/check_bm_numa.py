"""Check one host's 16 live BM pools and successful HAL NUMA selections."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

GIB = 1 << 30
CREATE = re.compile(
    r"Creating mempool BM pool:.*\btp_rank=(\d+) "
    r"local_numa_nodes=([0-9,]+) numa_node=(-?\d+) bm_flags=(\d+)"
)
HAL = re.compile(r"Try HalMemCreate ret:(-?\d+) numa:(\d+).*?\bsize:(\d+)")


def check_reports(directory: Path, rank: int, profile: str = "even") -> dict[str, Any]:
    """Require zero exits, peer probes, coexistence and the requested NUMA budget."""
    if profile == "even":
        expected_nodes = (0,) * 4 + (2,) * 4 + (4,) * 4 + (6,) * 4
        node_list = "0,2,4,6"
        gib_per_pool = 1
    elif profile == "all":
        expected_nodes = tuple(device // 2 for device in range(16))
        node_list = "0,1,2,3,4,5,6,7"
        gib_per_pool = 48
    else:
        raise ValueError(f"unknown NUMA profile: {profile}")
    contribution = gib_per_pool * GIB
    expected_counts = Counter(expected_nodes)
    errors = []
    counts: Counter[int] = Counter()
    workers = []
    exits = {}
    try:
        for line in (directory / "exits.tsv").read_text().splitlines():
            device, code = map(int, line.split())
            if device in exits:
                raise ValueError(f"duplicate exit status for device {device}")
            exits[device] = code
        if set(exits) != set(range(16)):
            raise ValueError("exits.tsv must contain exactly devices 0..15")
    except (OSError, ValueError) as error:
        errors.append(f"exit statuses: {error}")

    for device, expected_node in enumerate(expected_nodes):
        worker: dict[str, Any] = dict(
            device_id=device, expected_numa_node=expected_node, status="failed"
        )
        workers.append(worker)
        try:
            if exits.get(device) != 0:
                raise ValueError(f"worker exit={exits.get(device)}; expected 0")
            report = json.loads((directory / f"device-{device}.json").read_text())
            if report.get("status") != "passed" or report.get("rank") != rank:
                raise ValueError("missing successful report for this P/D rank")
            if report["layout"]["contributions"] != [contribution, contribution]:
                raise ValueError(
                    f"each side must contribute exactly {gib_per_pool} GiB per pool"
                )
            probes = [
                check
                for check in report.get("checks", [])
                if check.get("check") == "bm_peer_probe"
            ]
            if len(probes) != 1 or any(
                probes[0].get(name) != value
                for name, value in (
                    ("verified_bytes", 64),
                    ("device_id", device),
                    ("local_devices", list(range(16))),
                )
            ):
                raise ValueError("missing 64-byte peer probe for the 16-device run")
            log = (directory / f"device-{device}.log").read_text(errors="replace")
            requested = [
                (int(tp_rank), nodes, int(node), int(flags))
                for tp_rank, nodes, node, flags in CREATE.findall(log)
            ]
            if requested != [(device, node_list, expected_node, 0x80 | expected_node)]:
                raise ValueError(f"unexpected BM NUMA request: {requested}")
            attempts = [tuple(map(int, match)) for match in HAL.findall(log)]
            worker["hal_attempts"] = attempts
            if not attempts or any(
                node != expected_node or size != contribution
                for _, node, size in attempts
            ):
                raise ValueError(
                    f"HAL did not use NUMA {expected_node} for {gib_per_pool} GiB"
                )
            if sum(ret == 0 for ret, _, _ in attempts) != 1:
                raise ValueError("expected exactly one successful HAL allocation")
            ready = (
                f"[BM_STARTUP] LOCAL_POOLS_READY rank={rank} device={device} devices="
            )
            if (
                ready not in log
                or not (directory / "ready" / f"device-{device}.ready").is_file()
            ):
                raise ValueError("missing confirmation that all 16 pools coexisted")
            if f"[BM_STARTUP] PASSED rank={rank} device={device}\n" not in log:
                raise ValueError("worker did not finish the paired teardown")
            counts[expected_node] += 1
            worker["status"] = "passed"
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            worker["error"] = str(error)
            errors.append(f"device {device}: {error}")

    if counts != expected_counts:
        errors.append(
            f"expected successful pools by NUMA {dict(expected_counts)}, "
            f"got {dict(counts)}"
        )
    return dict(
        status="failed" if errors else "passed",
        rank=rank,
        profile=profile,
        local_dram_bytes_per_pool=contribution,
        expected_local_dram_bytes=16 * contribution,
        successful_pools_by_numa=dict(sorted(counts.items())),
        successful_dram_bytes_by_numa={
            node: count * contribution for node, count in sorted(counts.items())
        },
        workers=workers,
        errors=errors,
    )


def main() -> int:
    """Write an auditable summary and fail on incomplete or unbalanced runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("even", "all"), default="even")
    parser.add_argument("--rank", type=int, choices=(0, 1), required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = check_reports(args.report_dir, args.rank, args.profile)
    label = "EVEN_NUMA" if args.profile == "even" else "NUMA"
    filename = (
        "even-numa-summary.json" if args.profile == "even" else "numa-summary.json"
    )
    output = args.report_dir / filename
    output.write_text(json.dumps(summary, indent=2) + "\n")
    for worker in summary["workers"]:
        print(
            f"{label} device={worker['device_id']} "
            f"node={worker['expected_numa_node']} status={worker['status']}"
        )
    if summary["status"] != "passed":
        for error in summary["errors"]:
            print(error, file=sys.stderr)
        print(f"{label}_FAILED rank={args.rank} summary={output}")
        return 1
    print(
        f"ALL_{label}_CHECKS_PASSED rank={args.rank} "
        f"pools=16 counts={summary['successful_pools_by_numa']} summary={output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
