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
EXPECTED_NODES = (0, 2, 4, 6) * 4
CREATE = re.compile(
    r"Creating mempool BM pool:.*\btp_rank=(\d+) "
    r"local_numa_node_count=(\d+) numa_node=(-?\d+) bm_flags=(\d+)"
)
HAL = re.compile(r"Try HalMemCreate ret:(-?\d+) numa:(\d+).*?\bsize:(\d+)")


def check_reports(directory: Path, rank: int) -> dict[str, Any]:
    """Require zero exits, peer probes, coexistence and four pools per even node."""
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

    for device, expected_node in enumerate(EXPECTED_NODES):
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
            if report["layout"]["contributions"] != [GIB, GIB]:
                raise ValueError("each side must contribute exactly 1 GiB per pool")
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
            requested = [tuple(map(int, match)) for match in CREATE.findall(log)]
            if requested != [(device, 8, expected_node, 0x80 | expected_node)]:
                raise ValueError(f"unexpected BM NUMA request: {requested}")
            attempts = [tuple(map(int, match)) for match in HAL.findall(log)]
            worker["hal_attempts"] = attempts
            if not attempts or any(
                node != expected_node or size != GIB for _, node, size in attempts
            ):
                raise ValueError(f"HAL did not use NUMA {expected_node} for 1 GiB")
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

    if counts != Counter({0: 4, 2: 4, 4: 4, 6: 4}):
        errors.append(
            f"expected four successful pools on each even node, got {dict(counts)}"
        )
    return dict(
        status="failed" if errors else "passed",
        rank=rank,
        local_dram_bytes_per_pool=GIB,
        successful_pools_by_numa=dict(sorted(counts.items())),
        workers=workers,
        errors=errors,
    )


def main() -> int:
    """Write an auditable summary and fail on incomplete or unbalanced runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank", type=int, choices=(0, 1), required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = check_reports(args.report_dir, args.rank)
    output = args.report_dir / "even-numa-summary.json"
    output.write_text(json.dumps(summary, indent=2) + "\n")
    for worker in summary["workers"]:
        print(
            f"EVEN_NUMA device={worker['device_id']} "
            f"node={worker['expected_numa_node']} status={worker['status']}"
        )
    if summary["status"] != "passed":
        for error in summary["errors"]:
            print(error, file=sys.stderr)
        print(f"EVEN_NUMA_FAILED rank={args.rank} summary={output}")
        return 1
    print(
        f"ALL_EVEN_NUMA_CHECKS_PASSED rank={args.rank} "
        f"pools=16 counts={summary['successful_pools_by_numa']} summary={output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
