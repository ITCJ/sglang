"""Ticket04 evidence index: missing, failed and verified coverage stay distinct."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mempool_logical_cases import POLICY

NAMES = ["logic-a", "logic-b", "logic-c", "logic-e", "long-generation", "early-eos"]


def check_capacity_rejection(
    before: dict[str, Any],
    after: dict[str, Any],
    decode_log: str,
    *,
    run_id: str,
    http_status: int,
    worker_exit: int,
) -> dict[str, Any]:
    if (
        after["ready"]
        or "native capacities" not in str(after["fault"])
        or http_status != 503
        or type(worker_exit) is not int
        or worker_exit == 0
        or any(
            before[k] != after[k]
            for k in ("active", "ledger", "directory", "completed")
        )
        or any(after[k] for k in ("active", "ledger", "directory", "completed"))
    ):
        raise ValueError(
            "capacity rejection admitted work or changed its resource ledger"
        )
    proofs = [
        json.loads(line.split(" data=", 1)[1])
        for line in decode_log.splitlines()
        if "mempool acceptance_capacity data=" in line
    ]
    if not proofs or any(
        p.get("run_id") != run_id
        or p.get("rank") != 15
        or p.get("required_hbm_pages") != 65
        or p.get("advertised_hbm_pages") != 64
        or p.get("actual_hbm_pages", 0) < 65
        for p in proofs
    ):
        raise ValueError("missing actual/injected rank15 capacity evidence")
    return dict(
        status="passed",
        injection=proofs[-1],
        http_status=http_status,
        worker_exit=worker_exit,
        ledger_unchanged=True,
        coverage="real worker registration with an injected advertised limit",
    )


def summarize(root: Path) -> dict[str, Any]:
    """Index only final-version receipts produced by the strict per-stage checkers."""
    errors: list[str] = []

    def read(name: str) -> Any:
        path = root / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            return {}

    target = read("target/evidence.json")
    reference = read("reference/evidence.json")
    reference_answers = read("reference/answers.json")
    stability = read("reference/stability.json")
    answers = read("target/answers.json")
    fetch = read("fetch/decode-report.json")
    rejections = read("target/input-rejections.json")
    capacity = read("capacity/evidence.json")
    timeout = read("timeout/timeout.json")
    cleanup = [
        read(f"{phase}/process-cleanup.json")
        for phase in ("reference", "target", "capacity", "timeout")
    ]
    evidence = target if isinstance(target, dict) else {}
    prefix = evidence.get("prefix", {})
    requests = evidence.get("request_evidence", [])
    base = (
        evidence.get("status") == "passed"
        and evidence.get("requests") == 6
        and evidence.get("ranks_per_side") == 16
        and evidence.get("graph") is True
        and isinstance(requests, list)
        and len(requests) == 6
        and [r.get("case") for r in requests] == NAMES
        and all(
            [r.get("rank") for r in request.get("decode", [])] == list(range(16))
            for request in requests
        )
    )
    restored = (
        isinstance(prefix, dict)
        and prefix.get("status") == "passed"
        and prefix.get("ranks") == 16
        and prefix.get("main_layers") == 78
        and prefix.get("index_layers") == 21
    )
    good_answers = (
        isinstance(answers, list)
        and len(answers) == 6
        and [a.get("case") for a in answers] == NAMES
        and all(
            a.get("policy") == POLICY and a.get("status") == "passed" for a in answers
        )
    )
    reference_ok = (
        isinstance(reference, dict)
        and reference.get("status") == "formal_service_passed"
        and reference.get("decode_requests") == 6
        and reference.get("ranks_per_side") == 16
    )
    checks: dict[str, Any] = {}

    def record(
        key: str, sources: list[str], available: list[Any], passed: bool, detail: str
    ) -> None:
        checks[key] = dict(
            status="not-run"
            if any(v is None for v in available)
            else "passed"
            if passed
            else "failed",
            evidence=sources,
            detail=detail,
        )

    target_path = ["target/evidence.json"]
    record(
        "V1",
        target_path,
        [target],
        base and restored and prefix.get("hit_lengths") == [0, 4096, 3968, 0, 0, 0],
        "ABCE hit lengths 0/4096/3968/0, followed by two cold controls",
    )
    record(
        "V2",
        target_path,
        [target],
        base and restored,
        "all 16 ranks, 78 main layers and 21 Index K layers checked in logical token order",
    )
    record(
        "V3",
        ["reference/evidence.json", "target/answers.json"],
        [reference, answers],
        reference_ok and good_answers,
        "independent reference/target answers; full output ID differences reported separately",
    )
    baseline_failed = isinstance(reference_answers, list) and any(
        a.get("status") == "failed" for a in reference_answers
    )
    target_failed = isinstance(answers, list) and any(
        a.get("status") != "passed" for a in answers
    )
    if baseline_failed or target_failed:
        checks["V3"].update(
            status="failed",
            failure_category="baseline-failed" if baseline_failed else "target-failed",
            target_status="not-run" if answers is None else "recorded",
        )
        checks["V3"]["evidence"].append("reference/answers.json")
    fetch_ok = isinstance(fetch, dict) and fetch.get("status") == "passed"
    if fetch_ok:
        fetch_ok = {"p_miss", "d_miss", "mixed"} <= {
            c.get("case") for c in fetch.get("checks", [])
        }
    record(
        "V4",
        target_path + ["fetch/decode-report.json"],
        [target, fetch],
        base and fetch_ok,
        "actual service P/D sources plus P-only, D-only and mixed adapter results",
    )
    record(
        "V5",
        target_path,
        [target],
        base,
        "native_freed precedes finished in Main's causal trace; all-rank cleanup checked",
    )
    record(
        "V6",
        target_path,
        [target],
        base
        and restored
        and prefix.get("pages_per_request") == [32, 40, 40, 40, 40, 40],
        "whole-slot alive/native resources returned; prefix retained at 32/40/40/40 pages",
    )
    early = requests[-1] if base else {}
    record(
        "V7",
        target_path + ["target/answers.json"],
        [target, answers],
        base and restored and good_answers and 1 < early.get("output_tokens", 0) < 128,
        "C restores 31 full pages and recomputes 128 tokens; separate early-EOS cleanup",
    )
    rejects_ok = (
        isinstance(rejections, dict)
        and rejections.get("status") == "passed"
        and rejections.get("ledger_unchanged") is True
        and {"unaligned", "too-long", "generation-limit"}
        <= set(rejections.get("names", []))
    )
    record(
        "V8",
        ["target/input-rejections.json", "capacity/evidence.json"],
        [rejections, capacity],
        rejects_ok
        and isinstance(capacity, dict)
        and capacity.get("status") == "passed"
        and capacity.get("ledger_unchanged") is True
        and capacity.get("worker_exit") not in (None, 0),
        "three input limits plus isolated all-rank registration capacity rejection",
    )
    record(
        "V9",
        target_path,
        [target],
        base and restored,
        "E uses the previously freed minimum P/D slots; completion, free_ack and generations checked",
    )
    record(
        "V10",
        target_path,
        [target],
        base,
        "every actual decode step records one resident and one executing request",
    )
    load = requests[4] if base else {}
    load_ok = (
        base
        and load.get("output_tokens") == 128
        and all(d.get("forwards", 0) >= 127 for d in load.get("decode", []))
    )
    timeout_ok = (
        isinstance(timeout, dict)
        and timeout.get("status") == "passed"
        and timeout.get("process_cleanup_verified") is True
        and type(timeout.get("worker_exit")) is int
        and timeout["worker_exit"] != 0
    )
    cleanup_ok = all(
        isinstance(c, dict) and c.get("status") == "passed" and c.get("paired") is True
        for c in cleanup
    )
    stability_ok = isinstance(stability, dict) and stability.get("status") == "passed"
    passed = (
        all(c["status"] == "passed" for c in checks.values())
        and load_ok
        and timeout_ok
        and cleanup_ok
        and stability_ok
        and not errors
    )
    return dict(
        status="passed" if passed else "failed",
        coverage="complete sparse stage0/L0",
        policy=POLICY,
        checks=checks,
        long_generation="passed"
        if load_ok
        else "not-run"
        if target is None
        else "failed",
        timeout_fail_stop="passed"
        if timeout_ok
        else "not-run"
        if timeout is None
        else "failed",
        paired_cleanup="passed"
        if cleanup_ok
        else "not-run"
        if any(c is None for c in cleanup)
        else "failed",
        answers=answers,
        reference_answers=reference_answers,
        stability=stability if stability is not None else dict(status="not-run"),
        artifact_errors=errors,
        user_accepted=False,
        limitations=["Physical NUMA placement is not verified by these receipts."],
    )
