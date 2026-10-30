"""Ticket03 A/B/C evidence: native restore, suffix, delayed fill and exact contents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import verify_mempool_prefix as saved


def make_cases(model_path: str, page_size: int) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = saved.make_cases(model_path, page_size)[:3]
    for case in cases:
        case["name"] = case["name"].replace("cold-", "reuse-", 1)
    return cases


def check_reuse(
    prefill: Path,
    snapshots: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    full: bool = False,
) -> dict[str, Any]:
    count = len(cases) if full else 3
    if full and count not in (4, 6):
        raise ValueError(
            "full acceptance requires ABCE with optional load/EOS controls"
        )
    if len(snapshots) != count or len(cases) != count:
        raise ValueError(
            "reuse acceptance requires exactly A/B/C" + ("/E" if full else "")
        )
    a, b, c = [case["input_ids"] for case in cases[:3]]
    if list(map(len, (a, b, c))) != [4096, 5120, 4096] or a != b[:4096] or a != c:
        raise ValueError("reuse fixtures do not exercise A/B/C")
    if full and (
        len(cases[3]["input_ids"]) != 4096 or cases[3]["input_ids"][:128] == a[:128]
    ):
        raise ValueError("E must be an independent 4096-token cold request")
    if count == 6 and (
        [case["name"] for case in cases[4:]] != ["long-generation", "early-eos"]
        or [len(case["input_ids"]) for case in cases[4:]] != [4096, 128]
    ):
        raise ValueError("full controls must be long-generation and early-eos")
    hits = [0, 4096, 3968, 0, 0, 0][:count]
    totals = [32, 40, 40, 40, 40, 40][:count]
    reports = saved.Reports.read(prefill)
    progress: dict[tuple[str, int], list[tuple[int, dict[str, Any]]]] = {}
    for line_no, line in enumerate(prefill.read_text(errors="replace").splitlines()):
        if "mempool prefix_progress data=" in line:
            value = json.loads(line.split(" data=", 1)[1])
            progress.setdefault((value["rid"], value["rank"]), []).append(
                (line_no, value)
            )
    retained: dict[int, dict[str, str]] = {rank: {} for rank in range(16)}
    previous: dict[str, Any] = {}
    base_hashes: list[str] = []
    for snapshot, case, hit, added, total in zip(
        snapshots, cases, hits, (32, 8, 0, 0, 0, 0), totals
    ):
        done, directory = snapshot["completed"][-1], snapshot["directory"]
        if (
            done["hit_len"] != hit
            or snapshot["fault"]
            or snapshot["active"] is not None
        ):
            raise ValueError(
                "reuse request did not finish with its required hit length"
            )
        if (
            snapshot["ledger"]["p_prefix"]
            != dict(total=40, free=40 - total, outstanding=total)
            or len(directory) != total
        ):
            raise ValueError(
                "reuse prefix occupancy differs from the required sequence"
            )
        for key, prefix in previous.items():
            if key not in directory or directory[key]["prefix"] != prefix:
                raise ValueError("reuse changed a retained prefix location")
        free_op, published = saved.check_trace(done=done, added=added, total=total)
        saved.check_directory(
            directory=directory, previous=previous, published=published
        )
        if hit:
            check_operations(done)
        elif done["reads"]:
            raise ValueError("cold request unexpectedly restored a prefix")
        elif not previous:
            base_hashes = [item["hash"] for item in published]
        for rank in range(16):
            if hit:
                check_restored_rank(
                    reports,
                    progress,
                    snapshot,
                    previous,
                    base_hashes,
                    retained[rank],
                    rank,
                    len(case["input_ids"]),
                )
            saved.check_rank(
                reports=reports,
                snapshot=snapshot,
                prompt_tokens=len(case["input_ids"]),
                added=added,
                total=total,
                rank=rank,
                free_op=free_op,
                published=published,
                retained=retained[rank],
                restored=bool(hit),
            )
        previous = {key: node["prefix"] for key, node in directory.items()}
    if reports.consumed != set(reports.contents):
        raise ValueError("unexpected reuse request/rank content receipt")
    expected_progress = {
        (s["completed"][-1]["rid"], rank) for s in snapshots[1:3] for rank in range(16)
    }
    if set(progress) != expected_progress:
        raise ValueError("unexpected or missing reuse progress rank")
    return dict(
        status="passed",
        hit_lengths=hits,
        suffix_tokens=[len(case["input_ids"]) - hit for case, hit in zip(cases, hits)],
        pages_per_request=totals,
        ranks=16,
        main_layers=78,
        index_layers=21,
        noncontiguous_native_mapping=True,
        native_free_before_fill=True,
        contents="computed-source snapshots == restored native HBM; prefix/suffix reference coverage complete",
        bm_readback=False,
        unverified=[
            "alive DRAM contents",
            "prefix DRAM contents",
            "fill DRAM contents",
        ],
        acceptance_controls="one skipped allocator page; fill submitted after native_freed",
    )


def check_operations(done: dict[str, Any]) -> None:
    trace, reads = done["trace"], done["reads"]
    if set(reads) != {"load_back", "fill"}:
        raise ValueError("reuse requires two distinct prefix reads")

    def pos(kind: str, op_id: int | None = None) -> int:
        hits = [
            i
            for i, item in enumerate(trace)
            if item["kind"] == kind
            and item["role"] == "prefill"
            and (op_id is None or item["op_id"] == op_id)
        ]
        if len(hits) != 1:
            raise ValueError("missing or duplicate reuse event: " + kind)
        return hits[0]

    for kind, operation in reads.items():
        command, ack = operation["command"], operation["ack"]
        if (
            ack is None
            or command["kind"] != kind
            or command["rid"] != done["rid"]
            or command["room"] != done["room"]
            or command["role"] != "prefill"
            or command["hit_len"] != done["hit_len"]
            or ack["kind"] != kind + "_ack"
            or any(
                ack[k] != command[k]
                for k in ("run_id", "rid", "room", "attempt", "role", "op_id")
            )
            or [r["rank"] for r in ack["ranks"]] != list(range(16))
            or [r["rank"] for r in command["sources"]] != list(range(16))
        ):
            raise ValueError(
                "reuse command/ack differs from its request or rank coverage"
            )
        op_id = command["op_id"]
        if (
            not pos("prefix_protect", op_id)
            < pos(kind, op_id)
            < pos(kind + "_ack", op_id)
            < pos("prefix_unprotect", op_id)
        ):
            raise ValueError("prefix read protection/ack order is invalid")
    load, fill = (
        reads["load_back"]["command"]["op_id"],
        reads["fill"]["command"]["op_id"],
    )
    if load == fill or not (
        pos("load_back_ack")
        < pos("prefix_protect", fill)
        < pos("fill")
        < pos("prefix_unprotect", load)
        < pos("native_freed")
        < pos("fill_ack")
        < pos("prefix_unprotect", fill)
        < pos("ready_tokens")
    ):
        raise ValueError(
            "restore, independent fill, native free and ready lack causal ordering"
        )


def check_restored_rank(
    reports: saved.Reports,
    progress: dict[tuple[str, int], list[tuple[int, dict[str, Any]]]],
    snapshot: dict[str, Any],
    previous: dict[str, Any],
    base_hashes: list[str],
    retained: dict[str, str],
    rank: int,
    prompt: int,
) -> None:
    done = snapshot["completed"][-1]
    hit, rid = done["hit_len"], done["rid"]
    slot = done["allocations"]["prefill"][rank]["slot"]
    identity = dict(rid=rid, rank=rank, slot=slot, prompt_tokens=prompt)
    alive_line, alive = reports.content(stage="alive", **identity)
    suffix_line, suffix = reports.content(stage="suffix", **identity)
    native_line = reports.native.get((rank, done["room"]), -1)
    source_pages = [previous[h]["locations"][rank] for h in base_hashes[: hit // 128]]
    expected = {
        str(i): retained[str(p["logical_page"])] for i, p in enumerate(source_pages)
    }
    lines = {}
    for kind, operation in done["reads"].items():
        command, receipt = operation["command"], operation["ack"]["ranks"][rank]
        line, content = reports.content(stage=kind, **identity)
        lines[kind] = line
        pages = [p["logical_page"] for p in source_pages]
        if (
            command["sources"][rank] != dict(rank=rank, slot=slot, pages=source_pages)
            or receipt["readable"] is not True
            or receipt["slot"] != slot
            or receipt["tokens"] != hit
            or receipt["main_layers"] != 78
            or receipt["index_layers"] != 21
            or receipt["source_pages"] != pages
            or content["source_pages"] != pages
            or content["op_id"] != command["op_id"]
            or content["pages"] != expected
        ):
            raise ValueError(
                "reuse content/receipt differs from saved sources or request alive"
            )
        local = reports.events.get((rid, rank, kind), [])
        if not local or any(
            int(fields["op_id"]) != command["op_id"] or event_line <= line
            for event_line, fields in local
        ):
            raise ValueError("reuse rank acknowledged before content verification")
        if kind == "load_back":
            indices = content["prefix_indices"]
            if (
                content["parts"] != [512, 64]
                or indices != receipt["prefix_indices"]
                or len(indices) != hit
                or len(set(indices)) != hit
                or any(type(i) is not int or i < 128 for i in indices)
                or any(indices[i] % 128 for i in range(0, hit, 128))
                or any(
                    indices[i] != indices[i // 128 * 128] + i % 128 for i in range(hit)
                )
                or all(
                    indices[i] == indices[i - 128] + 128 for i in range(128, hit, 128)
                )
            ):
                raise ValueError(
                    "load_back lacks actual noncontiguous complete native pages"
                )
    if not lines["load_back"] < suffix_line < native_line < lines["fill"] < alive_line:
        raise ValueError("suffix/native free/fill order was not demonstrated")
    suffix_pages = list(range(hit // 128, prompt // 128))
    if (
        suffix["op_id"] != 0
        or suffix["source_pages"] != suffix_pages
        or suffix["pages"] != {str(p): alive["pages"][str(p)] for p in suffix_pages}
        or any(alive["pages"][key] != value for key, value in expected.items())
    ):
        raise ValueError("joined alive differs from original prefix/computed suffix")
    samples = progress.get((rid, rank), [])
    pending, complete, previous_completed = False, False, 0
    for line, value in samples:
        count, filled = value["suffix_completed"], value["fill_complete"]
        if (
            value["room"] != done["room"]
            or value["hit_len"] != hit
            or value["suffix_start"] != hit
            or not previous_completed <= count <= prompt - hit
            or value["ready_tokens"] != (hit + count if filled else 0)
            or (filled and line <= lines["fill"])
        ):
            raise ValueError(
                "prefix progress skipped its unfilled prefix or miscounted suffix"
            )
        previous_completed = count
        pending |= (
            count == prompt - hit
            and not filled
            and value["native_freed"]
            and native_line < line < lines["fill"]
        )
        complete |= count == prompt - hit and filled and value["native_freed"]
    if not pending or not complete:
        raise ValueError(
            "missing native-free/suffix-complete progress while fill was pending"
        )
