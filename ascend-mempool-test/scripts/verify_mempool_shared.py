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


def check_page_first_registration(
    registrations: dict[str, Any], config: RouterConfig
) -> dict[str, Any]:
    """Require actual per-region geometry and the peer startup signature."""
    pages = {}
    for role in ("prefill", "decode"):
        for row in registrations[role]["ranks"]:
            if row.get("storage_order") != "page_first":
                raise ValueError("registration is not page_first")
            signature = row["layout_signature"]["layout"]
            for geometry in (
                signature[name]
                for name in (
                    "prompt",
                    "decode",
                    "prompt_index",
                    "prefix_main",
                    "prefix_index",
                )
            ):
                if (
                    geometry["storage_order"] != "page_first"
                    or geometry["page_size"] != config.page_size
                ):
                    raise ValueError(
                        "startup layout signature differs from page_first geometry"
                    )
            for name, region in row["replica_locations"].items():
                index = name.endswith("index")
                prefix = name.startswith("prefix_")
                layers = len(row["indexer_layers"]) if index else row["layers"]
                dim = row["index_dim"] if index else row["dim"]
                expected = dict(
                    layers=layers,
                    slots=config.prefix_pages // len(config.prefill_groups)
                    if prefix
                    else row["slots"],
                    tokens=config.page_size
                    if prefix
                    else config.prompt_capacity
                    if role == "prefill"
                    else config.decode_storage_capacity,
                    heads=1,
                    dim=dim,
                    dtype=row["dtype"],
                    page_size=config.page_size,
                    storage_order="page_first",
                )
                if region.get("physical_layout") != expected:
                    raise ValueError("physical page-first region dimensions differ")
                page_bytes = layers * config.page_size * dim * 2
                if region.get("page_bytes") != page_bytes:
                    raise ValueError("page block byte count differs")
                pages[role + "/" + name] = page_bytes
    return dict(
        status="passed",
        storage_order="page_first",
        page_size=config.page_size,
        page_bytes=pages,
        startup_signature="present and matches regions",
    )


def check_registration(
    registrations: dict[str, Any], config: RouterConfig
) -> dict[str, Any]:
    groups = {rank: group for group in config.session_groups for rank in group}
    main: dict[tuple[int, int], dict[str, Any]] = {}
    index: dict[str, Any] | None = None
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
            session_members = tuple(range(16)) if config.prefill_index_shared else group
            participant = side * len(session_members) + session_members.index(rank)
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
                shared_index = name == "alive_index" and config.prefill_index_shared
                shared_prefix = name.startswith("prefix_") and config.prefix_shared
                members = (
                    list(range(16))
                    if shared_index
                    else list(group)
                    if shared or shared_prefix
                    else [rank]
                )
                owner = members[0]
                if (
                    value["owner"] != owner
                    or value["members"] != members
                    or value["participant"] != participant
                    or value["owner_participant"]
                    != side * len(session_members) + session_members.index(owner)
                    or value["world_size"] != 2 * len(session_members)
                    or value["numa_node"] != (0 if shared_index else row["numa_node"])
                    or not value["session"]
                ):
                    raise ValueError("shared owner/member/participant mismatch")
                if value["owner"] == rank:
                    owned += (
                        value["bytes"] // len(config.prefill_groups)
                        if shared_prefix
                        else value["bytes"]
                    )
                if shared_prefix:
                    expected = [
                        dict(
                            segment=i,
                            owner=g[0],
                            members=list(g),
                            numa_node=rows[g[0]]["numa_node"],
                            bytes=value["bytes"] // len(config.prefill_groups),
                        )
                        for i, g in enumerate(config.prefill_groups)
                    ]
                    if (
                        value.get("replica") != 0
                        or value.get("segments") != expected
                        or value.get("mapping_members") != list(range(16))
                        or value.get("device_mapping_members") != list(range(16))
                        or value.get("mapping_participants") != list(range(32))
                        or value["bytes"]
                        != (78 * 1152 if name == "prefix_main" else 21 * 256)
                        * config.prefix_pages
                        * config.page_size
                        or value["session"] != rows[0]["prefix_region"]
                        or row.get("prefix_policy") != config.prefix_policy()
                    ):
                        raise ValueError(
                            "shared prefix segment mapping differs from applied registration"
                        )
                    ranges = value.get("device_ranges", [])
                    if (
                        len(ranges) != len(expected)
                        or any(
                            r.get("segment") != segment["segment"]
                            or r.get("bytes") != segment["bytes"]
                            or not int(r.get("address", "0"), 16)
                            for r, segment in zip(ranges, expected)
                        )
                        or value.get("registration_seconds") is not None
                        or not value.get("timing_note")
                    ):
                        raise ValueError(
                            "shared prefix segment device ranges lack registration evidence"
                        )
                if shared:
                    old = main.setdefault((side, group[0]), value)
                    if any(
                        old[key] != value[key]
                        for key in ("session", "bytes", "numa_node", "members")
                    ):
                        raise ValueError(
                            "shared members describe different physical replicas"
                        )
                if shared_index:
                    if index is None:
                        index = value
                    if any(
                        index[key] != value[key]
                        for key in (
                            "session",
                            "bytes",
                            "numa_node",
                            "members",
                            "owner_participant",
                        )
                    ):
                        raise ValueError(
                            "shared Index K members describe different physical replicas"
                        )
            aligned = (owned + 64 * len(session_members) + 2**30 - 1) // 2**30 * 2**30
            if (
                (side == 1 and config.decode_shared)
                or (side == 0 and config.prefix_shared)
            ) and rank != group[0]:
                aligned = 0
            if row["local_dram_bytes"] != aligned:
                raise ValueError(
                    "BM allocation differs from owned shared/private payloads"
                )
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
        allocation_status="applied",
        main_replicas=p_count or 16,
        decode_main_replicas=d_count or 16,
        index_replicas=1 if index is not None else 16,
        prefix_replicas=1 if config.prefix_shared else 16,
        per_numa=numa_by_role["prefill"],
        decode_per_numa=numa_by_role["decode"],
        physical_placement_verified=placement,
        allocation_evidence="BM local_mem_size matched each participant contribution at join",
        resident_page_limit="NUMA allocation/registration is separate from physical page residence",
        prefix_segments=registrations["prefill"]["ranks"][0]["replica_locations"][
            "prefix_main"
        ]["segments"]
        if config.prefix_shared
        else [],
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
    prefix_free = set(range(config.prefix_pages))
    for request_index, (snapshot, case) in enumerate(zip(snapshots, cases)):
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
        if config.prefix_shared:
            evidence[-1]["prefix"] = check_prefix_operations(
                snapshot, config, tokens, request_index, prefix_free
            )
        if config.prefill_index_shared:
            index_rows = ready[-1].get("index_writer_rows", [])
            if (
                len(index_rows) != 16
                or ready[-1].get("index_completed_pages") != tokens // config.page_size
            ):
                raise ValueError("single Index K lacks completed write coverage")
            for rank in range(16):
                writer = PageWriter(
                    config.prefill_index_writers.index(rank)
                    if rank in config.prefill_index_writers
                    else -1,
                    len(config.prefill_index_writers),
                    config.page_size,
                )
                if index_rows[rank] != writer.rows(hit, tokens, tokens):
                    raise ValueError(
                        "actual Index K writes differ from writer assignment"
                    )
                if hit and (
                    fill is None
                    or fill["ack"]["ranks"][rank].get("index_rows")
                    != writer.rows(0, hit, tokens)
                ):
                    raise ValueError(
                        "actual Index K fill differs from writer assignment"
                    )
            evidence[-1]["index_written_rows"] = index_rows
            evidence[-1]["index_written_bytes"] = [n * 21 * 256 for n in index_rows]
    return dict(
        status="passed",
        registration=registration,
        writes=evidence,
        cross_writer_read="all P and D ranks used their own device mapping and unidex_copy",
        evidence_scope="startup sentinel rows and completed model writes; no extra model-prefix DRAM readback",
    )


def check_prefix_operations(
    snapshot: dict[str, Any],
    config: RouterConfig,
    tokens: int,
    request_index: int,
    free: set[int],
) -> dict[str, Any]:
    """Count actual completed page transfers separately from collective padding."""
    from ascend_mempool.prefix_layout import PrefixLayout

    layout = PrefixLayout(config.prefix_pages, config.prefill_groups)
    done = snapshot["completed"][-1]
    registration = snapshot["registrations"]["prefill"]["ranks"][0]
    row_bytes = dict(
        main=registration["layers"] * registration["dim"] * 2,
        index=len(registration["indexer_layers"]) * registration["index_dim"] * 2,
    )
    evidence = {}
    if done["admit"] is not None:
        operation = done["admit"]
        ranks = operation["ack"]["ranks"]
        start = (
            config.prefix_start_segment
            if config.prefix_start_segment is not None
            else request_index % len(layout.groups)
        )
        targets = layout.select(free, len(operation["command"]["source_pages"]), start)
        for rank, receipt in enumerate(ranks):
            expected = [
                p
                for p in targets
                if (
                    config.prefix_admit_rank
                    if config.prefix_admit_mode == "single_rank"
                    else layout.groups[layout.decode(p)[0]][0]
                )
                == rank
            ]
            if (
                receipt.get("target_pages") != targets
                or receipt.get("written_pages") != expected
                or receipt.get("start_segment") != start
                or any(
                    receipt.get(region + "_bytes")
                    != len(expected) * config.page_size * size
                    for region, size in row_bytes.items()
                )
            ):
                raise ValueError("prefix admit allocation/writer/bytes mismatch")
        free.difference_update(targets)
        evidence["admit"] = dict(
            start_segment=start,
            target_pages=targets,
            main_bytes=len(targets) * config.page_size * row_bytes["main"],
            index_bytes=len(targets) * config.page_size * row_bytes["index"],
        )
    for kind, operation in done["reads"].items():
        ranks = operation["ack"]["ranks"]
        sources = [
            p["logical_page"] for p in operation["command"]["sources"][0]["pages"]
        ]
        for region, size in row_bytes.items():
            for rank, receipt in enumerate(ranks):
                pages = receipt.get("read_pages", {}).get(region, [])
                if kind == "fill":
                    group = (
                        next(g for g in layout.groups if rank in g)
                        if region == "main"
                        else config.prefill_index_writers
                    )
                    writer = PageWriter(
                        group.index(rank) if rank in group else -1,
                        len(group),
                        config.page_size,
                        config.prefill_write_assignment
                        if region == "main"
                        else "contiguous",
                    )
                    expected = [
                        p for i, p in enumerate(sources) if i in writer.pages(tokens)
                    ]
                    if sorted(pages) != sorted(expected):
                        raise ValueError(
                            "prefix fill source pages differ from full-prompt writer assignment"
                        )
                elif config.read_assignment == "by_segment" and any(
                    rank not in layout.groups[layout.decode(p)[0]] for p in pages
                ):
                    raise ValueError(
                        "prefix load_back reader is outside its source segment"
                    )
                counts = [
                    sum(layout.decode(p)[0] == segment for p in pages)
                    * config.page_size
                    for segment in range(len(layout.groups))
                ]
                if (
                    receipt.get("segment_rows", {}).get(region) != counts
                    or receipt.get(region + "_bytes")
                    != len(pages) * config.page_size * size
                ):
                    raise ValueError("prefix read segment/byte counts mismatch")
            if kind == "load_back" and sorted(
                p for receipt in ranks for p in receipt["read_pages"][region]
            ) != sorted(sources):
                raise ValueError(
                    "prefix load_back must read each physical page exactly once"
                )
        if kind == "load_back":
            width = max(len(r["read_pages"]["main"]) for r in ranks)
            if any(
                r.get("gather_width_pages") != width
                or r["read_pages"]["main"] != r["read_pages"]["index"]
                for r in ranks
            ):
                raise ValueError(
                    "prefix collective width or main/Index K assignment mismatch"
                )
        evidence[kind] = {
            region + "_bytes": sum(r[region + "_bytes"] for r in ranks)
            for region in row_bytes
        }
        evidence[kind]["segment_rows"] = {
            region: [
                sum(r["segment_rows"][region][s] for r in ranks)
                for s in range(len(layout.groups))
            ]
            for region in row_bytes
        }
    return evidence
