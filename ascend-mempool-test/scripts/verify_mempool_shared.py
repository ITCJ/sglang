"""Check ticket05 ownership, cross-member reads and actual write/fill counts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import RouterConfig
    from sglang.srt.hardware_backend.npu.mempool.page_write import PageWriter
else:
    from ascend_mempool.page_write import PageWriter
    from ascend_mempool_pd.mempool_router_protocol import RouterConfig


def check_registration(
    registrations: dict[str, Any], config: RouterConfig
) -> dict[str, Any]:
    groups = {rank: group for group in config.session_groups for rank in group}
    main: dict[tuple[int, int], dict[str, Any]] = {}
    numa_by_role: dict[str, dict[int, dict[str, int]]] = {
        role: {} for role in ("prefill", "decode")
    }
    placement = True
    for side, role in enumerate(("prefill", "decode")):
        rows = registrations[role]["ranks"]
        if [r["rank"] for r in rows] != list(range(16)):
            raise ValueError("shared registration lacks TP16 ranks")
        for row in rows:
            rank, group = row["rank"], groups[row["rank"]]
            participant = side * len(group) + group.index(rank)
            if row["replicas"] != config.replicas(role):
                raise ValueError("shared replica counts differ from configuration")
            regions = row["replica_locations"]
            if set(regions) != (
                {"alive_main", "alive_index", "prefix_main", "prefix_index"}
                if side == 0
                else {"alive_main"}
            ):
                raise ValueError("shared registration lacks storage regions")
            owned = 0
            for name, value in regions.items():
                shared = (config.prefill_shared, config.decode_shared)[
                    side
                ] and name == "alive_main"
                if (
                    value["owner"] != (group[0] if shared else rank)
                    or value["members"] != (list(group) if shared else [rank])
                    or value["participant"] != participant
                    or value["owner_participant"]
                    != (side * len(group) if shared else participant)
                    or value["world_size"] != 2 * len(group)
                    or value["numa_node"] != row["numa_node"]
                    or not value["session"]
                ):
                    raise ValueError("shared owner/member/participant mismatch")
                if value["owner"] == rank:
                    owned += value["bytes"]
                if shared:
                    old = main.setdefault((side, group[0]), value)
                    if any(
                        old[key] != value[key]
                        for key in ("session", "bytes", "numa_node", "members")
                    ):
                        raise ValueError(
                            "shared members describe different physical replicas"
                        )
            aligned = (owned + 64 * len(group) + 2**30 - 1) // 2**30 * 2**30
            if side == 1 and config.decode_shared and rank != group[0]:
                aligned = 0
            if row["local_dram_bytes"] != aligned:
                raise ValueError(
                    "BM allocation differs from owned shared/private payloads"
                )
            probe = row.get("shared_probe", {})
            if config.prefill_shared and (
                probe["status"] != "passed"
                or probe["reader"] != rank
                or probe["writers"] != list(group)
                or probe["copy"] != "unidex_copy"
                or probe["source"] != "shared_prompt"
                or probe["bytes"] != len(group) * row["dim"] * 2
            ):
                raise ValueError("missing live cross-writer shared prompt read")
            if side == 1 and config.decode_shared and config.verify_decode:
                order = probe.get("decode_order", {})
                if (
                    order.get("status") != "passed"
                    or order.get("engine") != "graph_replay"
                    or not order.get("negative_without_wait")
                    or not order.get("positive_with_wait")
                ):
                    raise ValueError("missing shared decode Graph ordering proof")
            node = numa_by_role[role].setdefault(
                row["numa_node"],
                dict(main_replicas=0, main_payload_bytes=0, allocated_bytes=0),
            )
            node["allocated_bytes"] += row["local_dram_bytes"]
            if rank == regions["alive_main"]["owner"]:
                node["main_replicas"] += 1
                node["main_payload_bytes"] += regions["alive_main"]["bytes"]
            if row["local_dram_bytes"]:
                placement = placement and bool(row["numa_placement_verified"])
    p_count = sum(side == 0 for side, _ in main)
    d_count = sum(side == 1 for side, _ in main)
    if p_count != (
        len(config.prefill_groups) if config.prefill_shared else 0
    ) or d_count != (len(config.decode_groups) if config.decode_shared else 0):
        raise ValueError("physical main replica count is wrong")
    return dict(
        status="passed",
        main_replicas=p_count or 16,
        decode_main_replicas=d_count or 16,
        per_numa=numa_by_role["prefill"],
        decode_per_numa=numa_by_role["decode"],
        physical_placement_verified=placement,
        allocation_evidence="BM local_mem_size matched each participant contribution at join",
        resident_page_limit="NUMA allocation/registration is separate from physical page residence",
    )


def check_stage(root: Path, config: RouterConfig) -> dict[str, Any]:
    from verify_mempool_session import log_records

    registration = check_registration(
        json.loads((root / "registrations.json").read_text()), config
    )
    snapshots = json.loads((root / "snapshots.json").read_text())
    cases = json.loads((root / "responses.json").read_text())["cases"]
    log = root / "prefill-normal.log"
    if not log.exists():
        log = root / "prefill.log"
    records = log_records(log, "ready_accounting")
    evidence = []
    for snapshot, case in zip(snapshots, cases):
        done = snapshot["completed"][-1]
        tokens, hit = len(case["input_ids"]), done["hit_len"]
        ready = [
            r
            for r in records
            if r["rid"] == done["rid"] and r["ready_tokens"] == tokens
        ]
        if not ready:
            raise ValueError("shared prompt never became completely ready")
        rows = ready[-1].get("writer_rows", [])
        fill = done["reads"].get("fill")
        for group in config.prefill_groups if config.prefill_shared else ():
            covered: list[int] = []
            descriptors = [done["allocations"]["prefill"][r]["pages"] for r in group]
            if any(p != descriptors[0] for p in descriptors):
                raise ValueError("writers do not share their logical page descriptors")
            for member, rank in enumerate(group):
                writer = PageWriter(
                    member,
                    len(group),
                    config.page_size,
                    config.prefill_write_assignment,
                )
                covered.extend(writer.pages(tokens))
                if rows[rank] != writer.rows(hit, tokens, tokens):
                    raise ValueError(
                        "actual prefill writes differ from page assignment"
                    )
                if hit and (
                    fill is None
                    or fill["ack"]["ranks"][rank]["main_rows"]
                    != writer.rows(0, hit, tokens)
                ):
                    raise ValueError("actual fill writes differ from page assignment")
            if sorted(covered) != list(range(tokens // config.page_size)):
                raise ValueError("shared writers overlap or leave missing pages")
        evidence.append(
            dict(
                rid=done["rid"],
                written_rows=rows,
                written_bytes=[n * 78 * 1152 for n in rows],
                hit_tokens=hit,
            )
        )
    return dict(
        status="passed",
        registration=registration,
        writes=evidence,
        cross_writer_read="all P and D ranks used their own device mapping and unidex_copy",
        evidence_scope="startup sentinel rows and completed model writes; no extra model-prefix DRAM readback",
    )
