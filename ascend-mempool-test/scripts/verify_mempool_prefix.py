"""Ticket02 cold-save fixtures and content/ownership evidence checks."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import msgspec
import verify_mempool_min


class Reports(msgspec.Struct):
    contents: dict[tuple[str, int, str], tuple[int, dict[str, Any]]] = msgspec.field(
        default_factory=dict
    )
    events: dict[tuple[str, int, str], list[tuple[int, dict[str, str]]]] = (
        msgspec.field(default_factory=dict)
    )
    native: dict[tuple[int, int], int] = msgspec.field(default_factory=dict)
    consumed: set[tuple[str, int, str]] = msgspec.field(default_factory=set)

    @classmethod
    def read(cls, prefill: Path) -> Reports:
        report = cls()
        for line_no, line in enumerate(
            prefill.read_text(errors="replace").splitlines()
        ):
            if "mempool prefix_content data=" in line:
                value = json.loads(line.split(" data=", 1)[1])
                content_key = value["rid"], value["rank"], value["stage"]
                if content_key in report.contents:
                    raise ValueError("duplicate prefix content receipt")
                report.contents[content_key] = line_no, value
            fields = dict(re.findall(r"(\w+)=([^\s,]+)", line))
            if fields.get("role") != "prefill":
                continue
            if "mempool native_free " in line:
                report.native[int(fields["rank"]), int(fields["room"])] = line_no
            if "mempool sr " in line:
                report.events.setdefault(
                    (fields["rid"], int(fields["rank"]), fields["event"]), []
                ).append((line_no, fields))

        return report

    def content(
        self, *, stage: str, rid: str, rank: int, slot: int, prompt_tokens: int
    ) -> tuple[int, dict[str, Any]]:
        key = rid, rank, stage
        if key not in self.contents:
            raise ValueError("missing rank content stage: " + stage)
        self.consumed.add(key)
        line_no, value = self.contents[key]
        if (
            value["slot"] != slot
            or value["prompt_tokens"] != prompt_tokens
            or value["main_layers"] != 78
            or value["index_layers"] != 21
            or value["page_size"] != 128
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                for digest in value["pages"].values()
            )
        ):
            raise ValueError("content receipt differs from actual source/geometry")
        return line_no, value


def make_cases(model_path: str, page_size: int) -> list[dict[str, Any]]:
    from transformers import AutoTokenizer

    if page_size != 128:
        raise ValueError("prefix acceptance requires B=128")
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    base = verify_mempool_min.make_cases(model_path, page_size)[0]
    a = base["input_ids"]
    extension = tokenizer.encode(
        "\nAdditional context. Compute 9 times 7. ", add_special_tokens=False
    )
    different = tokenizer.encode(
        "A different document about trees and rivers. ", add_special_tokens=False
    )
    if not extension or not different or different[0] == a[0]:
        raise ValueError("tokenizer did not produce an independent E prefix")
    b = a + (extension * (1024 // len(extension) + 1))[:1024]
    e = (different * (4096 // len(different) + 1))[:4096]
    return [
        dict(
            name="cold-prefix-" + name,
            input_ids=ids,
            rendered_input=tokenizer.decode(ids),
            sampling_params=base["sampling_params"].copy(),
        )
        for name, ids in (("a", a), ("b", b), ("c", a.copy()), ("e", e))
    ]


def check_prefix(
    prefill: Path, snapshots: list[dict[str, Any]], cases: list[dict[str, Any]]
) -> dict[str, Any]:
    """Join content, real allocation, all-rank ack and publication by request/op/page."""
    if len(snapshots) != 4 or len(cases) != 4:
        raise ValueError("prefix acceptance requires all A/B/C/E cases")
    a, b, c, e = [case["input_ids"] for case in cases]
    if (
        list(map(len, (a, b, c, e))) != [4096, 5120, 4096, 4096]
        or b[:4096] != a
        or c != a
        or e[:128] == a[:128]
    ):
        raise ValueError("prefix fixtures do not exercise A/B/C/E")
    reports = Reports.read(prefill)
    retained: dict[int, dict[str, str]] = {rank: {} for rank in range(16)}
    previous: dict[str, Any] = {}
    for snapshot, case, added, total in zip(
        snapshots, cases, (32, 8, 0, 0), (32, 40, 40, 40)
    ):
        done, directory = snapshot["completed"][-1], snapshot["directory"]
        if done["hit_len"] != 0 or snapshot["fault"] or snapshot["active"] is not None:
            raise ValueError("prefix acceptance must finish a cold request")
        quota = snapshot["ledger"]["p_prefix"]
        if (
            quota != dict(total=40, free=40 - total, outstanding=total)
            or len(directory) != total
        ):
            raise ValueError("prefix occupancy differs from 32/40/40/40")
        for key, prefix in previous.items():
            if key not in directory or directory[key]["prefix"] != prefix:
                raise ValueError("a retained directory location was overwritten")
        free_op, published = check_trace(done=done, added=added, total=total)
        check_directory(directory=directory, previous=previous, published=published)
        for rank in range(16):
            check_rank(
                reports=reports,
                snapshot=snapshot,
                prompt_tokens=len(case["input_ids"]),
                added=added,
                total=total,
                rank=rank,
                free_op=free_op,
                published=published,
                retained=retained[rank],
            )
        previous = {key: node["prefix"] for key, node in directory.items()}
    if reports.consumed != set(reports.contents):
        raise ValueError("unexpected request/rank content receipt")
    return dict(
        status="passed",
        pages_per_request=[32, 40, 40, 40],
        ranks=16,
        main_layers=78,
        index_layers=21,
        cold_only=True,
        contents="original == alive == prefix == retained after alive free",
    )


def check_trace(
    *, done: dict[str, Any], added: int, total: int
) -> tuple[int, list[dict[str, Any]]]:
    trace = done["trace"]
    rid = done["rid"]

    def position(kind: str, role: str = "prefill") -> int:
        hits = [
            i
            for i, item in enumerate(trace)
            if item["kind"] == kind and item["role"] == role
        ]
        if len(hits) != 1:
            raise ValueError("missing or duplicate lifecycle event: " + kind)
        return hits[0]

    free_op = trace[position("free")]["op_id"]
    if (
        not position("native_freed")
        < position("finished", "decode")
        < position("free")
        < position("free_ack")
    ):
        raise ValueError("native/finished/free order lacks the required causal path")
    operation = done["admit"]
    published = [item for item in trace if item["kind"] == "publish"]
    source_pages = list(range(total - added, total)) if added else []
    if added:
        if not operation or len(published) != added:
            raise ValueError("missing prefix admit/publication")
        command, ack = operation["command"], operation["ack"]
        op_id = command["op_id"]
        if (
            command["source_pages"] != source_pages
            or command["hashes"] != [item["hash"] for item in published]
            or any(
                ack[key] != command[key]
                for key in ("rid", "room", "attempt", "op_id", "role")
            )
            or ack["kind"] != "admit_ack"
            or command["rid"] != rid
            or [item["rank"] for item in ack["ranks"]] != list(range(16))
        ):
            raise ValueError("prefix command/ack identity or page coverage differs")
        if not (
            position("finished", "decode")
            < position("source_protect")
            < position("admit")
            < position("owner_release")
            < position("admit_ack")
            < position("free")
        ):
            raise ValueError("prefix source/owner/ack ordering is invalid")
        for index, item in enumerate(trace):
            if item["kind"] == "publish" and not (
                position("admit_ack") < index < position("free")
                and item["op_id"] == op_id
            ):
                raise ValueError("prefix published before matching all-rank ack")
    elif (
        operation is not None
        or published
        or any(item["kind"] in ("admit", "admit_ack") for item in trace)
    ):
        raise ValueError("duplicate/full-capacity request performed a save")

    return free_op, published


def check_directory(
    *,
    directory: dict[str, Any],
    previous: dict[str, Any],
    published: list[dict[str, Any]],
) -> None:
    seen = set(previous)
    for item in published:
        node = directory[item["hash"]]
        if node["parent_hash"] is not None and node["parent_hash"] not in seen:
            raise ValueError("child published before its parent")
        seen.add(item["hash"])
    children = {key: 0 for key in directory}
    for key, node in directory.items():
        if node["parent_hash"] is not None:
            children[node["parent_hash"]] += 1
        if (
            node["state"] != "freed"
            or node["owner"] is not None
            or node["lock_ref"]
            or node["source_ops"]
            or node["locations"]
            or node["prefix"]["state"] != "ready"
            or node["prefix"]["lock_ops"]
            or len(node["prefix"]["locations"]) != 16
        ):
            raise ValueError("directory retains request ownership or an unready prefix")
    if any(node["child_count"] != children[key] for key, node in directory.items()):
        raise ValueError("directory child count differs from actual children")


def check_rank(
    *,
    reports: Reports,
    snapshot: dict[str, Any],
    prompt_tokens: int,
    added: int,
    total: int,
    rank: int,
    free_op: int,
    published: list[dict[str, Any]],
    retained: dict[str, str],
    restored: bool = False,
) -> None:
    done = snapshot["completed"][-1]
    rid, room = done["rid"], done["room"]
    slot = done["allocations"]["prefill"][rank]["slot"]
    operation = done["admit"]
    source_pages = list(range(total - added, total)) if added else []
    op_id = operation["command"]["op_id"] if operation else None
    identity = dict(rid=rid, rank=rank, slot=slot, prompt_tokens=prompt_tokens)
    alive_line, alive = reports.content(stage="alive", **identity)
    retained_line, saved = reports.content(stage="retained", **identity)
    expected_sources = list(range(prompt_tokens // 128))
    free = reports.events.get((rid, rank, "free"), [])
    native_line = reports.native.get((rank, room), -1)
    alive_native_order = (
        native_line < alive_line if restored else alive_line < native_line
    )
    if (
        set(alive["pages"]) != set(map(str, expected_sources))
        or alive["source_pages"] != expected_sources
        or alive["op_id"] != 0
        or saved["op_id"] != 0
        or saved["source_pages"]
        or len(free) != 1
        or int(free[0][1]["op_id"]) != free_op
        or native_line < 0
        or not alive_native_order
        or not max(alive_line, native_line) < retained_line < free[0][0]
    ):
        raise ValueError(
            "content evidence is incomplete or crosses the release boundary"
        )
    if added:
        admit_line, copied = reports.content(stage="admit", **identity)
        receipt = operation["ack"]["ranks"][rank]
        targets = receipt["target_pages"]
        if (
            copied["op_id"] != op_id
            or copied["source_pages"] != source_pages
            or receipt["slot"] != slot
            or receipt["source_pages"] != source_pages
            or receipt["readable"] is not True
            or receipt["main_layers"] != 78
            or receipt["index_layers"] != 21
            or len(targets) != added
            or len(receipt["pages"]) != added
            or len(set(targets)) != added
            or any(type(page) is not int or not 0 <= page < 40 for page in targets)
            or targets != operation["ack"]["ranks"][0]["target_pages"]
            or set(copied["pages"]) != set(map(str, targets))
            or set(copied["pages"]).intersection(retained)
        ):
            raise ValueError("admit content differs from actual copy receipt")
        local_admit = reports.events.get((rid, rank, "admit"), [])
        if (
            not local_admit
            or not reports.native[rank, room] < admit_line < retained_line
            or any(
                not admit_line < line < retained_line or int(fields["op_id"]) != op_id
                for line, fields in local_admit
            )
        ):
            raise ValueError("admit acknowledged without completed content evidence")
        check_copied_pages(
            operation=operation,
            done=done,
            rank=rank,
            slot=slot,
            snapshot=snapshot,
            published=published,
            copied=copied,
            alive=alive,
        )
        retained.update(copied["pages"])
    if saved["pages"] != retained or len(saved["pages"]) != total:
        raise ValueError("prefix contents changed after alive free or a later request")


def check_copied_pages(
    *,
    operation: dict[str, Any],
    done: dict[str, Any],
    rank: int,
    slot: int,
    snapshot: dict[str, Any],
    published: list[dict[str, Any]],
    copied: dict[str, Any],
    alive: dict[str, Any],
) -> None:
    source_pages = operation["command"]["source_pages"]
    receipt = operation["ack"]["ranks"][rank]
    directory = snapshot["directory"]
    for logical, target, item, descriptor in zip(
        source_pages, receipt["target_pages"], published, receipt["pages"]
    ):
        source_descriptor = done["allocations"]["prefill"][rank]["pages"][logical]
        if (
            operation["command"]["sources"][rank]
            != dict(
                rank=rank,
                slot=slot,
                pages=[
                    done["allocations"]["prefill"][rank]["pages"][p]
                    for p in source_pages
                ],
            )
            or source_descriptor["logical_page"] != slot * 64 + logical
            or copied["pages"][str(target)] != alive["pages"][str(logical)]
            or descriptor
            != dict(
                pool="prefix",
                region=snapshot["registrations"]["prefill"]["ranks"][rank]["session"],
                replica=rank,
                logical_page=target,
                page_size=128,
            )
            or directory[item["hash"]]["prefix"]["locations"][rank] != descriptor
        ):
            raise ValueError("copied values or published pages differ from allocation")
