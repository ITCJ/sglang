"""Offline checker for archived ticket11 copy traces from pre-cleanup versions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def verify(root, phases=("target", "restart"), page_size=128):
    reports = []
    for phase in phases:
        submitted, completed, receipts = {}, {}, {}
        order = {}
        path = root / phase / "prefill-normal.log"
        if not path.exists():
            path = root / phase / "prefill.log"
        for path in (path,):
            for position, line in enumerate(
                path.read_text(errors="replace").splitlines()
            ):
                match = re.search(
                    r"mempool sr role=prefill rank=(\d+) event=(admit|fill) rid=(\S+) room=\S+ attempt=\S+ op_id=(\d+)",
                    line,
                )
                if match:
                    receipts[match[3], int(match[4]), int(match[1])] = position
                for marker, output in (
                    ("pool_copy_submit", submitted),
                    ("pool_copy_complete", completed),
                ):
                    prefix = f"mempool {marker} data="
                    if prefix in line:
                        row = json.loads(line.split(prefix, 1)[1])
                        key = row["rid"], row["op_id"], row["rank"]
                        output[key] = row
                        order[key, marker] = position
        if not submitted:
            raise ValueError(
                f"{phase}: no pool-copy submissions; new path was not exercised"
            )
        for key, row in submitted.items():
            done = completed.get(key)
            if done is None or done["state"] != "READY":
                raise ValueError(f"{phase}: missing library completion {key}")
            if not (
                order[key, "pool_copy_submit"]
                < order[key, "pool_copy_complete"]
                < receipts.get(key, -1)
            ):
                raise ValueError(f"completion/receipt order mismatch {key}")
            if row["blocks"]:
                if done.get("delay_seconds") != 0.05:
                    raise ValueError("required copy delay was not applied")
                if (
                    done["ready_at"] - done["delay_started"] < done["delay_seconds"]
                    or done.get("poll_count", 0) < 2
                ):
                    raise ValueError(
                        f"delay did not hold completion while tick polled {key}"
                    )
            if row["blocks"] and (
                done["submit_return"] != 0 or done["wait_return"] != 0
            ):
                raise ValueError("SDK completion failed")
        if {row["kind"] for row in submitted.values()} != {"admit", "fill"}:
            raise ValueError("both admit and fill must be exercised")
        # Base acceptance already checks Main trace: ack -> publish -> free -> free_ack.
        groups = {}
        for key, row in submitted.items():
            groups.setdefault(key[:2], []).append(row)
        expected_ops = {}
        for snapshot in json.loads((root / phase / "snapshots.json").read_text()):
            for done in snapshot["completed"]:
                if done.get("admit"):
                    command = done["admit"]["command"]
                    expected_ops[done["rid"], command["op_id"]] = (
                        "admit",
                        len(command["source_pages"]),
                    )
                if done.get("reads", {}).get("fill"):
                    command = done["reads"]["fill"]["command"]
                    expected_ops[done["rid"], command["op_id"]] = (
                        "fill",
                        done["hit_len"] // page_size,
                    )
        if set(groups) != set(expected_ops):
            raise ValueError("copy operations differ from Main request history")
        for key, rows in groups.items():
            if {r["rank"] for r in rows} != set(range(16)):
                raise ValueError(f"incomplete shard receipts {key}")
            blocks = sum(r["blocks"] for r in rows)
            size = sum(r["bytes"] for r in rows)
            kind = rows[0]["kind"]
            factor = 2 if kind == "admit" else 9
            pages, extra = divmod(blocks, factor)
            expected = pages * (
                (78 * page_size * 1152 + 21 * page_size * 256)
                if kind == "admit"
                else (8 * 78 * page_size * 1152 + 21 * page_size * 256)
            )
            if (
                (kind, pages) != expected_ops[key]
                or extra
                or pages == 0
                or size != expected
            ):
                raise ValueError(f"page block/byte accounting mismatch {key}")
        reports.append(dict(phase=phase, operations=len(groups), shards=len(submitted)))
    return reports


def main():
    p = argparse.ArgumentParser(
        description="Check archived pre-cleanup pool-copy reports"
    )
    p.add_argument("--report-root", type=Path, required=True)
    p.add_argument("--page-size", type=int, choices=(64, 128), default=128)
    args = p.parse_args()
    print(json.dumps(verify(args.report_root, page_size=args.page_size), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
