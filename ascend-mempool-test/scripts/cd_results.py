"""Atomic per-case checkpoints and strict C/D sample admission."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ascend_mempool.cd_plan import CDCase, expected_bytes
from ascend_mempool.sharing import group_sample


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def unit(case: CDCase, side: int) -> str:
    return f"{case.name}-side-{side}"


def load_reused(
    path: Path,
    contract: dict[str, Any],
    case: CDCase,
    side: int,
    member: int,
    environment: dict[str, Any],
) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(path.read_text())
    admit(record, contract, case, side, member)
    if record["environment"] != environment:
        raise ValueError("C/D resume environment changed")
    record["reused_from"] = str(path)
    return record


def admit(
    record: dict[str, Any],
    contract: dict[str, Any],
    case: CDCase,
    side: int,
    member: int,
) -> None:
    if (
        record.get("contract") != contract
        or record.get("status") != "checked"
        or record.get("case") != case.signature()
        or record.get("consumer_side") != side
    ):
        raise ValueError(
            "C/D checkpoint identity, configuration or correctness differs"
        )
    if type(record.get("numa_verified")) is not bool or not record.get("environment"):
        raise ValueError("C/D checkpoint lacks placement/environment evidence")
    rows = record.get("samples", [])
    active = contract["side"] == side
    if len(rows) != (contract["samples"] if active else 0):
        raise ValueError("C/D checkpoint has incomplete samples")
    if {r["sample"] for r in rows} != (
        set(range(contract["samples"])) if active else set()
    ):
        raise ValueError("C/D checkpoint sample indices differ")
    tp_size, mode = contract["topology"]["tp_size"], contract["topology"]["mode"]
    payload, logical = expected_bytes(case, tp_size, mode, member)
    for row in rows:
        if any(
            row.get(k) != v
            for k, v in dict(
                member=member,
                side=side,
                repeats=contract["repeats"],
                copy_bytes=payload,
                logical_bytes=logical,
            ).items()
        ):
            raise ValueError(
                "C/D sample rank, direction, repeat count or payload differs"
            )
        if row.get("opposite_side_idle") is not True or row.get("accepted") is not True:
            raise ValueError("C/D sample lacks isolated concurrent execution")
        if (
            not all(
                math.isfinite(row[k]) and row[k] > 0
                for k in ("device_ms", "start_ns", "done_ns")
            )
            or row["done_ns"] <= row["start_ns"]
        ):
            raise ValueError("invalid C/D timing")


def local_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        raise ValueError("C/D group needs every local rank")
    first = records[0]
    contract = first["contract"]
    n = contract["topology"]["tp_size"]
    if len(records) != n or {r["contract"]["member"] for r in records} != set(range(n)):
        raise ValueError("C/D group needs every local rank")
    case = CDCase(**first["case"])
    for record in records:
        c = record["contract"]
        if any(
            c[k] != contract[k]
            for k in (
                "measurement",
                "code",
                "topology",
                "round",
                "side",
                "samples",
                "repeats",
                "warmup",
            )
        ):
            raise ValueError("C/D checkpoint group mixes configurations")
        admit(record, c, case, first["consumer_side"], c["member"])
    if contract["side"] != first["consumer_side"]:
        return []
    result = []
    for sample in range(contract["samples"]):
        rows = [
            next(r for r in record["samples"] if r["sample"] == sample)
            for record in records
        ]
        if len({r["attempt"] for r in rows}) != 1:
            raise ValueError("C/D group mixes different launch attempts")
        group = group_sample(rows, n)
        if not group["aligned_batches"]:
            raise ValueError("C/D saved sample has no aligned concurrency")
        result.append(
            dict(
                **group,
                case=case.signature(),
                topology=contract["topology"]["name"],
                tp_size=n,
                round=contract["round"],
                side=contract["side"],
                sample=sample,
                repeats=contract["repeats"],
                group_ms=group["host_envelope_ms"] / contract["repeats"],
                device_per_call_ms=[
                    r["device_ms"] / r["repeats"]
                    for r in sorted(rows, key=lambda r: r["member"])
                ],
                numa_verified=all(r["numa_verified"] for r in records),
            )
        )
    return result
