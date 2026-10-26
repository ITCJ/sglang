"""Cold request fixtures and hardware evidence checks for mempool-min ticket01."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from verify_service import check_completion, check_resources

PRODUCERS = [0, 1, 2, *range(6, 75, 4)]


def make_cases(model_path: str, page_size: int) -> list[dict[str, Any]]:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    cases = []
    for name, expression, length, limit, ignore_eos in (
        ("cold-a", "17 + 25", 4096, 128, True),
        ("cold-b", "9 * 7", 4096, 128, True),
        ("early-eos", "1 + 1", page_size, 128, False),
        ("first-token", "17 + 25", 4096, 1, False),
    ):
        marker = "FILLER_MARKER"
        rendered = tokenizer.apply_chat_template(
            [
                dict(
                    role="user",
                    content=f"{marker}\nCompute {expression}. Reply only with the number.",
                )
            ],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        before, after = rendered.split(marker)
        prefix = tokenizer.encode(before, add_special_tokens=False)
        suffix = tokenizer.encode(after, add_special_tokens=False)
        padding = tokenizer.encode(
            " Background information. ", add_special_tokens=False
        )
        count = length - len(prefix) - len(suffix)
        if count < 0 or not padding:
            raise ValueError("tokenizer template does not fit the acceptance input")
        ids = prefix + (padding * (count // len(padding) + 1))[:count] + suffix
        if len(ids) % page_size:
            raise ValueError("acceptance prompt must align to page_size")
        cases.append(
            dict(
                name=name,
                input_ids=ids,
                rendered_input=tokenizer.decode(ids),
                sampling_params=dict(
                    temperature=0,
                    top_k=1,
                    top_p=1,
                    max_new_tokens=limit,
                    ignore_eos=ignore_eos,
                ),
            )
        )
    return cases


def check_response(case: dict[str, Any], result: dict[str, Any]) -> None:
    ids, meta = result.get("output_ids"), result.get("meta_info", {})
    if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
        raise ValueError("missing full output token IDs")
    if meta.get("prompt_tokens") != len(case["input_ids"]) or meta.get(
        "completion_tokens"
    ) != len(ids):
        raise ValueError("actual prompt/output length differs from the test case")
    limit = case["sampling_params"]["max_new_tokens"]
    if case["sampling_params"]["ignore_eos"] or limit == 1:
        if len(ids) != limit:
            raise ValueError("forced-length/first-token case ended at the wrong length")
    elif (
        not 1 <= len(ids) < limit or meta.get("finish_reason", {}).get("type") != "stop"
    ):
        raise ValueError(
            "early-eos case must actually stop before the generation limit"
        )


def check_target(
    prefill: Path,
    decode: Path,
    snapshots: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    page_size: int,
    prompt_capacity: int,
    decode_capacity: int,
    prefix: bool = False,
) -> dict[str, Any]:
    """Require per-rank Graph, transfer, completion and explicit release evidence."""
    ranks = set(range(16))
    resources: dict[tuple[str, int], Any] = {}
    completions: dict[tuple[int, int], Any] = {}
    histories: dict[tuple[str, int, int], list[str]] = {}
    mapping: dict[str, set[int]] = {role: set() for role in ("prefill", "decode")}
    captured, replayed = set(), set()
    native_bytes: dict[tuple[int, str], int] = {}
    steps: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for role, path in (("prefill", prefill), ("decode", decode)):
        for line in path.read_text(errors="replace").splitlines():
            if any(
                marker in line
                for marker in (
                    "Traceback (most recent call last)",
                    "fail-stop:",
                    "watchdog expired",
                    "mempool KV fetch invalid",
                    "mempool TP tick failed",
                )
            ):
                raise ValueError(f"worker failure in {path}: {line}")
            fields = dict(re.findall(r"(\w+)=([^\s,]+)", line.split(" data=", 1)[0]))
            if "mempool graph_captured" in line:
                captured.add(int(fields["device"].split(":")[-1]))
                if int(fields.get("batch_rows", 0)) != 16:
                    raise ValueError(
                        "capture does not record the configured padded width"
                    )
            if "mempool graph_replay" in line:
                if int(fields["real_requests"]) != 1:
                    raise ValueError("more than one real request entered Graph replay")
                replayed.add(int(fields["device"].split(":")[-1]))
            if fields.get("role") != role or "rank" not in fields:
                continue
            rank = int(fields["rank"])
            if "mempool mapping_ready" in line:
                mapping[role].add(rank)
            if "mempool resources " in line:
                data = json.loads(line.split(" data=", 1)[1])
                check_resources(role, data)
                if data["model_layers"] != 78 or data["indexer_layer_ids"] != PRODUCERS:
                    raise ValueError("worker is not the registered GLM-5.2 layout")
                resources[role, rank] = data
            if "mempool native_copy " in line:
                if int(fields["main_kv_bytes"]) != 0:
                    raise ValueError("native transfer unexpectedly copied main KV")
                key = (rank, fields["kind"])
                native_bytes[key] = native_bytes.get(key, 0) + int(fields["bytes"])
            if "room" not in fields:
                continue
            room = int(fields["room"])
            if "mempool decode_step " in line:
                steps.setdefault((rank, room), []).append(
                    (
                        int(fields["step"]),
                        int(fields["resident_requests"]),
                        int(fields["real_requests"]),
                    )
                )
            events = histories.setdefault((role, rank, room), [])
            if "mempool decode_completion " in line:
                if (rank, room) in completions:
                    raise ValueError("duplicate native completion receipt")
                completions[rank, room] = json.loads(line.split(" data=", 1)[1])
            for marker in ("native_free", "native_transfer_ready"):
                if f"mempool {marker} " in line:
                    events.append(marker)
            if "mempool sr " in line:
                events.append(fields["event"])
    if (
        captured != ranks
        or replayed != ranks
        or any(value != ranks for value in mapping.values())
    ):
        raise ValueError("incomplete mapping/Graph capture/replay rank coverage")
    if any((role, rank) not in resources for role in mapping for rank in ranks):
        raise ValueError("incomplete physical resource reports")
    if any(
        native_bytes.get((rank, kind), 0) <= 0
        for rank in ranks
        for kind in ("index_k", "aux")
    ):
        raise ValueError("missing actual native Index K/handoff transfers")
    if len(snapshots) != len(cases):
        raise ValueError("missing request ledger snapshots")
    source_rows = dict(prompt=0, decode=0)
    previous: dict[tuple[str, int], int] = {}
    for index, (snapshot, case) in enumerate(zip(snapshots, cases)):
        check_pages(snapshot, case, page_size, prompt_capacity, decode_capacity)
        if (
            snapshot["fault"]
            or snapshot["active"] is not None
            or (snapshot["directory"] and not prefix)
        ):
            raise ValueError("request returned before directory/resources were cleared")
        if any(
            v["free"] != v["total"] or v["outstanding"] != 0
            for name, v in snapshot["ledger"].items()
            if not (prefix and name == "p_prefix")
        ):
            raise ValueError("resource ledger did not return to its initial values")
        done = snapshot["completed"][-1]
        room = done["room"]
        check_response(case, done["result"])
        trace = [(t["role"], t["kind"]) for t in done["trace"]]
        for side in ("prefill", "decode"):
            if trace.index(("decode", "finished")) >= trace.index((side, "free_ack")):
                raise ValueError("alive freed before D finished")
        if (prefix or case["name"].startswith("cold")) and trace.index(
            ("prefill", "native_freed")
        ) >= trace.index(("decode", "finished")):
            raise ValueError(
                "P native resources were not demonstrably released before D finished"
            )
        for rank in ranks:
            p_alloc = done["allocations"]["prefill"][rank]
            native = done["allocations"]["decode"][rank]["native"]
            p, d = p_alloc["message"]["p_slot"], p_alloc["message"]["d_slot"]
            if (
                native["pages"]
                != (
                    len(case["input_ids"])
                    + case["sampling_params"]["max_new_tokens"]
                    + page_size
                    - 1
                )
                // page_size
                or native["row"] <= 0
            ):
                raise ValueError("incorrect actual native reservation")
            for side, lease in (("prefill", p), ("decode", d)):
                if lease["slot"] != 0 or lease["generation"] <= previous.get(
                    (side, rank), 0
                ):
                    raise ValueError("minimum slot was not physically freed and reused")
                previous[side, rank] = lease["generation"]
                events = histories.get((side, rank, room), [])
                if (
                    "native_free" not in events
                    or "free" not in events
                    or events.index("native_free") >= events.index("free")
                ):
                    raise ValueError("SR free lacks earlier native cleanup on a rank")
            data = completions.get((rank, room))
            if data is None:
                raise ValueError("missing D completion report")
            check_step_counts(steps.get((rank, room), []), data["forwards"])
            check_completion(
                data, 78, (p["slot"], p["generation"], d["slot"], d["generation"])
            )
            if not data.get("source_evidence"):
                raise ValueError("P/D source evidence was not captured")
            source_rows["prompt"] += data["prompt_bm_rows"]
            source_rows["decode"] += data["decode_bm_rows"]
            if (prefix or case["name"].startswith("cold")) and data["forwards"] < 1:
                raise ValueError("cold request never executed decode")
            if case["name"] == "first-token" and data["forwards"] != 0:
                raise ValueError("max_new_tokens=1 unexpectedly executed decode")
    if any(count <= 0 for count in source_rows.values()):
        raise ValueError("service has no completed BM reads from one of P/D")
    return dict(
        status="passed",
        coverage="ticket01 cold requests only",
        requests=len(cases),
        ranks_per_side=16,
        source_rows=source_rows,
        graph=True,
        page_size=page_size,
        prefix="not implemented in ticket01",
        numa_placement="not verified",
    )


def check_pages(
    snapshot: dict[str, Any],
    case: dict[str, Any],
    page_size: int,
    prompt_capacity: int,
    decode_capacity: int,
) -> None:
    """Audit the real registrations, logical pages and native reservation."""
    done = snapshot["completed"][-1]
    for role, capacity, ledger in (
        ("prefill", prompt_capacity, "p_alive"),
        ("decode", decode_capacity, "d_alive"),
    ):
        registrations = snapshot["registrations"][role]["ranks"]
        allocations = done["allocations"][role]
        if len(registrations) != 16 or len(allocations) != 16:
            raise ValueError("incomplete page_size rank evidence")
        if snapshot["ledger"][ledger]["total"] != 16 * capacity // page_size:
            raise ValueError("alive ledger uses a different token page_size")
        for rank, (registered, allocated) in enumerate(zip(registrations, allocations)):
            if (
                registered["rank"] != rank
                or allocated["rank"] != rank
                or registered["page_size"] != page_size
                or registered["hbm_page_size"] != page_size
            ):
                raise ValueError("DRAM/HBM page_size registration mismatch")
            pages = allocated["pages"]
            start = allocated["slot"] * (capacity // page_size)
            if len(pages) != capacity // page_size or any(
                p["page_size"] != page_size or p["logical_page"] != start + i
                for i, p in enumerate(pages)
            ):
                raise ValueError("SR logical pages differ from the whole-slot budget")
            if role == "decode":
                needed = (
                    len(case["input_ids"])
                    + case["sampling_params"]["max_new_tokens"]
                    + page_size
                    - 1
                ) // page_size
                if (
                    allocated["native"]["pages"] != needed
                    or allocated["native"]["tokens"] != needed * page_size
                ):
                    raise ValueError(
                        "native reservation does not use unified page_size"
                    )


def check_step_counts(samples: list[tuple[int, int, int]], forwards: int) -> None:
    if samples != [(step, 1, 1) for step in range(1, forwards + 1)]:
        raise ValueError("missing/invalid per-step D resident and execution counts")


def check_timeout(
    before: dict[str, Any], after: dict[str, Any], decode_log: str
) -> dict[str, Any]:
    """An expected timeout must preserve actual D allocations until process exit."""
    active = before.get("active")
    if not active or len(active["allocations"].get("decode", [])) != 16:
        raise ValueError("timeout test did not obtain all actual D native allocations")
    rid, room = active["rid"], active["room"]
    if (
        "timeout" not in str(after.get("fault")).lower()
        or after["ready"]
        or not after["active"]
        or after["active"]["rid"] != rid
        or after["ledger"] != before["ledger"]
        or after["directory"] != before["directory"]
        or after["completed"] != before["completed"]
        or after["active"]["free_acks"]
        or any(
            t["kind"] in ("finished", "free", "free_ack")
            for t in after["active"]["trace"]
        )
    ):
        raise ValueError("timeout released resources or continued the request")
    if any(q["outstanding"] <= 0 for q in after["ledger"].values()):
        raise ValueError("timeout did not retain the full reservation")
    allocated = set()
    for line in decode_log.splitlines():
        fields = dict(re.findall(r"(\w+)=([^\s,]+)", line))
        if fields.get("room") != str(room):
            continue
        if "mempool native_free " in line or fields.get("event") == "free":
            raise ValueError("worker freed native/alive resources during fail-stop")
        if (
            "mempool sr " in line
            and fields.get("event") == "alloc"
            and fields.get("rid") == rid
        ):
            if fields.get("free") != "15":
                raise ValueError("D allocation did not hold an alive slot")
            allocated.add(int(fields["rank"]))
    if allocated != set(range(16)):
        raise ValueError("timeout test lacks per-rank actual D allocation evidence")
    return dict(
        status="passed",
        rid=rid,
        room=room,
        reason=after["fault"],
        retained_native_ranks=16,
        retained_all_quotas=True,
        automatic_free=False,
    )
