"""Ticket25 single-service functional smoke, with explicit hardware limits."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from verify_mempool_session import accounting, log_records
from verify_mempool_shared import check_page_first_registration, check_registration


def check_stage(root: Path, config: Any) -> dict[str, Any]:
    """Check completed real requests, publication, and actual write counters."""
    snapshots = json.loads((root / "snapshots.json").read_text())
    cases = json.loads((root / "responses.json").read_text())["cases"]
    hits = [0, 4096, 4096 - config.page_size, 0, 0, 0]
    if len(cases) != 6 or len(snapshots) != 6:
        raise ValueError("dual-write smoke requires six completed service requests")
    expected = Counter()
    previous: dict[str, Any] = {}
    for index, (snapshot, case, hit) in enumerate(zip(snapshots, cases, hits)):
        done = snapshot["completed"][-1]
        if done["hit_len"] != hit or set(done["reads"]) != (
            {"load_back"} if hit else set()
        ):
            raise ValueError("dual-write hit/load_back mismatch or unexpected fill")
        if hit and (
            done["reads"]["load_back"]["ack"] is None
            or len(done["reads"]["load_back"]["ack"]["ranks"]) != 16
        ):
            raise ValueError("load_back lacks all-rank completion")
        directory = snapshot["directory"]
        retained = 4096 // config.page_size if index == 0 else 5120 // config.page_size
        if len(directory) != retained or snapshot["ledger"]["p_prefix"] != dict(
            total=config.prefix_pages,
            free=config.prefix_pages - retained,
            outstanding=retained,
        ):
            raise ValueError("candidate publication or full-pool fallback mismatch")
        if any(
            directory.get(key, {}).get("prefix") != value
            for key, value in previous.items()
        ):
            raise ValueError("published prefix locations changed")
        if any(node["prefix"]["lock_ops"] for node in directory.values()):
            raise ValueError("prefix request references survived drain")
        operation = done["admit"]
        if operation is None:
            raise ValueError("missing candidate publish completion")
        command, ack = operation["command"], operation["ack"]
        targets = command["target_pages"]
        added = (4096 if index == 0 else 1024 if index == 1 else 0) // config.page_size
        if (
            len(targets) != added
            or len(set(targets)) != added
            or command["sources"]
            or ack is None
            or [r["rank"] for r in ack["ranks"]] != list(range(16))
        ):
            raise ValueError(
                "zero-copy publication lacks expected candidate/all-rank evidence"
            )
        trace = [(event["role"], event["kind"]) for event in done["trace"]]
        if (
            not trace.index(("decode", "finished"))
            < trace.index(("prefill", "admit"))
            < trace.index(("prefill", "admit_ack"))
            < trace.index(("prefill", "free"))
        ):
            raise ValueError("publish/free happened before drain or completion")
        candidate_rows = [0] * 16
        per_segment = config.prefix_pages // len(config.prefill_groups)
        for page in targets:
            candidate_rows[config.prefill_groups[page // per_segment][0]] += (
                config.page_size
            )
        for rank, receipt in enumerate(ack["ranks"]):
            owned = [
                p for p in targets if config.prefill_groups[p // per_segment][0] == rank
            ]
            if (
                receipt["target_pages"] != targets
                or receipt["written_pages"] != owned
                or receipt["main_bytes"] != 0
                or receipt["index_bytes"] != 0
            ):
                raise ValueError("published candidate ownership/byte receipt mismatch")
        from ascend_mempool.page_write import PageWriter

        for group in config.prefill_groups:
            for member, rank in enumerate(group):
                writer = PageWriter(
                    member,
                    len(group),
                    config.page_size,
                    config.prefill_write_assignment,
                )
                expected[
                    writer.rows(hit, len(case["input_ids"]), len(case["input_ids"])),
                    candidate_rows[rank],
                ] += 1
        previous = {key: node["prefix"] for key, node in directory.items()}
    records = log_records(root / "prefill.log", "dual_write_complete")
    actual = Counter()
    for row in records:
        remote, candidate = row["main_remote_rows"], row["candidate_main_rows"]
        if (
            row["p_alive_bytes"] != 0
            or row["completion"] != "forward_event"
            or row["main_remote_bytes"] != remote * 78 * 1152
            or row["candidate_main_bytes"] != candidate * 78 * 1152
            or row["candidate_index_rows"] != candidate
            or row["candidate_index_bytes"] != candidate * 21 * 256
        ):
            raise ValueError("dual-write completed row/byte counters mismatch")
        actual[remote, candidate] += 1
    if actual != expected:
        raise ValueError(
            f"dual-write completed counters differ: actual={actual}, expected={expected}"
        )
    return dict(
        status="passed",
        hit_lengths=hits,
        fill=False,
        requests=6,
        writer_reports=len(records),
        publication="after drain; candidate owner receipts checked",
    )


def summarize(root: Path, config: Any) -> dict[str, Any]:
    stage = root / "target"

    def read(name: str) -> Any:
        return json.loads((stage / name).read_text())

    evidence = read("evidence.json")
    if evidence["status"] != "passed" or evidence["prefix"]["status"] != "passed":
        raise ValueError("incomplete dual-write service evidence")
    answers = read("answers.json")
    if len(answers) != 6 or any(row["status"] != "passed" for row in answers):
        raise ValueError("logical answer check failed")
    for name in (
        "process-cleanup.json",
        "ports-released.json",
        "capacity-replay.json",
        "input-rejections.json",
    ):
        if read(name)["status"] != "passed":
            raise ValueError(f"{name} failed")
    registrations = read("registrations.json")
    return dict(
        status="passed",
        coverage="Ticket25 dual-write functional smoke",
        service_rounds=1,
        observe=config.observe,
        accounting=accounting(stage, shared=True),
        registration=check_registration(registrations, config),
        page_first=check_page_first_registration(registrations, config),
        dual_write=evidence["prefix"],
        user_accepted=False,
        limitations=[
            "No performance, compute-overlap, physical NUMA placement or isolated remote-write visibility test",
            "No restart/fault injection in this smoke",
            "Completed counters and model reads; no exhaustive DRAM byte readback",
        ],
    )
