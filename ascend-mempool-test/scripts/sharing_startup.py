"""Small startup checks shared by the four-participant worker and its runner."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable


def wait_for_release(output: Path, release: Path, timeout: float) -> None:
    """Keep this pool live until BOTH hosts have all selected pools allocated."""
    (output / "ready").touch(exist_ok=False)
    deadline = time.monotonic() + timeout
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("host runner did not release the live shared pools")
        time.sleep(0.1)


def wait_for_workers(
    outputs: list[Path], timeout: float, check: Callable[[], None]
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        check()
        missing = [str(path) for path in outputs if not (path / "ready").exists()]
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"shared pools did not become ready: {missing}")
        time.sleep(0.1)


def check_reports(
    reports: list[dict[str, Any]], plan: dict[str, Any], side: int
) -> dict[str, Any]:
    """Reject partial runs, duplicate owners, wrong capacity and early teardown."""
    expected = {
        device: (group, member)
        for group in plan["groups"]
        for member, device in enumerate(group["devices"])
    }
    if len(reports) != len(expected):
        raise RuntimeError("missing or extra shared-pool worker reports")
    seen = set()
    nodes = []
    for report in reports:
        config = report["config"]
        device = config["device_id"]
        if device not in expected or device in seen:
            raise RuntimeError("unexpected or duplicate shared-pool device")
        seen.add(device)
        group, member = expected[device]
        participant = side * 2 + member
        allocation = plan["contributions"][participant]
        mapping = report["mapping"]
        if (
            report.get("status") != "startup_passed"
            or report.get("closed") is not True
            or report.get("cleanup_phase") != "closed"
            or report.get("startup")
            != dict(probes_verified=True, all_pools_live=True, participants=4)
            or config["mode"] != "shared"
            or config["side"] != side
            or config["member"] != member
            or config["numa_node"] != group["numa_node"]
            or config["plan"] != plan["plan"]
            or mapping["tp_size"] != 2
            or mapping["participant"] != participant
            or mapping["logical_side"] != side
            or mapping["member"] != member
            or mapping["local_dram_bytes"] != allocation
            or mapping["rank_stride_bytes"] != plan["bytes_per_numa"]
            or mapping["requested_numa_node"] != group["numa_node"]
            or set(mapping["owners"]) != {"0", "1"}
        ):
            raise RuntimeError(f"invalid shared startup evidence for device {device}")
        if member == 0:
            evidence = mapping["numa"]
            if not evidence["placement_verified"] and (
                evidence["status"] != "unobservable_driver_mapping"
            ):
                raise RuntimeError(f"NUMA placement failed for device {device}")
            nodes.append(
                dict(
                    numa_node=group["numa_node"],
                    devices=group["devices"],
                    owner=device,
                    bytes=allocation,
                    placement_verified=evidence["placement_verified"],
                )
            )
    total = sum(node["bytes"] for node in nodes)
    if total != plan["bytes_per_host"]:
        raise RuntimeError("shared-pool host capacity differs from the plan")
    return dict(
        status="startup_passed",
        scope=plan["scope"],
        nodes=sorted(nodes, key=lambda node: node["numa_node"]),
        bytes_per_host=total,
        numa_placement_verified=all(node["placement_verified"] for node in nodes),
        full_content_graph_performance_acceptance="not run",
    )
