"""Strict reuse of completed rounds, with explicit source provenance."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

# Audited migration: the new pipeline order changes only publication=True with
# synthetic delay. Timed benchmark calls use publication=False; no sample path
# changes. Do not extend this list without reviewing measurement compatibility.
COMPATIBLE_SOURCE = {
    "git_sha": "622c93f88e47fd77348aac2d1c4494008ea10485",
    "source_sha256": "3504edb85b8e63176dad78c29aac74421afa10738541ac2a23d8167ff13627d2",
}


# Lock the destination of the audited migration as well as its source. Tests and
# this admission module are excluded; all benchmark/runtime/orchestration Python
# files are covered. Any later implementation change needs a new audit.
COMPATIBLE_TARGET = "7975273fe63763dd48aad8a1bd75894b553805b8b423ea417d5c64ee9a8008ed"


def implementation_fingerprint() -> str:
    repo = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for root in (
        repo / "ascend-mempool-test",
        repo / "python/sglang/srt/hardware_backend/npu",
    ):
        for path in sorted(root.rglob("*.py")):
            if (
                path.resolve() == Path(__file__).resolve()
                or "tests" in path.relative_to(root).parts
            ):
                continue
            digest.update(str(path.relative_to(repo)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def load_completed_rounds(
    args: Any, plan: dict[str, Any], code: dict[str, str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not args.resume_from:
        return {}, {}
    source = args.log_root / args.resume_from / args.role
    identity = json.loads((source / "identity.json").read_text())
    if identity.get("run_id") != args.resume_from or identity.get("plan") != plan:
        raise ValueError("resume source run/plan differs; cannot mix measurements")
    if identity.get("code") not in (code, COMPATIBLE_SOURCE):
        raise ValueError("resume source code has no audited measurement compatibility")
    if identity["code"] != code and implementation_fingerprint() != COMPATIBLE_TARGET:
        raise ValueError(
            "current implementation differs from the audited resume target"
        )
    tp_size = plan["plan"].get("tp_size", 2)
    reports_by_phase = {}
    environments: dict[int, dict[str, Any]] = {}
    for round_id, order in enumerate(plan["mode_order"][: args.resume_round]):
        for mode in order:
            phase = f"round-{round_id}/{mode}"
            reports = []
            for member in range(tp_size):
                report = json.loads(
                    (source / phase / f"member-{member}/report.json").read_text()
                )
                config = report["config"]
                expected = dict(
                    plan=plan["plan"],
                    mode=mode,
                    round=round_id,
                    member=member,
                    side=0 if args.role == "prefill" else 1,
                    device_id=args.devices[member],
                    numa_node=args.numa_node,
                    prefill_host=args.prefill_host,
                    decode_host=args.decode_host,
                    topk=args.topk,
                    block_dims=args.block_dims,
                    samples=args.samples,
                    repeats=args.repeats,
                    warmup=args.warmup,
                    delay_matmuls=args.delay_matmuls,
                    smoke=False,
                    diagnostic=False,
                    identity=f"{args.resume_from}/{phase}",
                )
                if any(config.get(k) != v for k, v in expected.items()):
                    raise ValueError(
                        f"resume configuration mismatch: {phase}/member-{member}"
                    )
                if (
                    report.get("status")
                    not in ("checks_passed", "checks_passed_numa_unverified")
                    or report.get("closed") is not True
                ):
                    raise ValueError(
                        f"resume requires completed and closed workers: {phase}/member-{member}"
                    )
                rows = report.get("delayed_publication", [])
                if (
                    len(rows) != 4
                    or {(r["engine"], r["variant"]) for r in rows}
                    != {(e, v) for e in ("eager", "graph") for v in (0, 1)}
                    or any(
                        r.get("publication_readback_passed") is not True for r in rows
                    )
                ):
                    raise ValueError(
                        f"resume publication evidence incomplete: {phase}/member-{member}"
                    )
                if member == tp_size - 1 and any(
                    r.get("selected_before_writer_done") is not True
                    or not r.get("writer_minus_selected_ms", 0) > 0
                    for r in rows
                    if r["engine"] == "eager"
                ):
                    raise ValueError(
                        f"resume publication order failed: {phase}/member-{member}"
                    )
                for row in rows:
                    if row["engine"] == "eager":
                        selected, writer, gap = (
                            row[k]
                            for k in (
                                "selected_ms",
                                "writer_ms",
                                "writer_minus_selected_ms",
                            )
                        )
                        if (
                            not all(math.isfinite(v) for v in (selected, writer, gap))
                            or min(selected, writer) < 0
                            or not math.isclose(gap, writer - selected)
                        ):
                            raise ValueError(
                                f"invalid resume timing: {phase}/member-{member}"
                            )
                for engine in ("eager", "graph"):
                    trace = (
                        source / phase / f"member-{member}/delayed-{engine}-trace.json"
                    )
                    if not trace.is_file() or trace.stat().st_size == 0:
                        raise ValueError(f"resume trace missing: {trace}")
                environment = report.get("environment", {})
                required = {
                    "target_memfabric",
                    "memfabric",
                    "torch",
                    "torch_npu",
                    "sgl_kernel_npu",
                    "unidex_schema",
                    "device",
                    "hostname",
                    "kernel",
                    "cpu_affinity",
                    "/usr/local/Ascend/driver/version.info",
                    "/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
                }
                if not required <= environment.keys():
                    raise ValueError(
                        f"resume environment incomplete: {phase}/member-{member}"
                    )
                check_resume_environment(environment, environments.get(member))
                environments[member] = environment
                reports.append(report)
            reports_by_phase[phase] = reports
    return reports_by_phase, dict(
        source_root=str(source),
        source_identity=identity,
        reused_phases=list(reports_by_phase),
        environments=[environments[m] for m in range(tp_size)],
        measurement_compatibility="identical source"
        if identity["code"] == code
        else "audited 622c93f88e migration; timed benchmark unchanged",
        note="Source traces/logs remain in source_root; retain that directory",
    )


def check_resume_environment(actual: dict[str, Any], expected: Any) -> None:
    """Called by each new worker before BM initialization or any measurement."""
    if expected is not None and actual != expected:
        changed = sorted(
            k
            for k in actual.keys() | expected.keys()
            if actual.get(k) != expected.get(k)
        )
        raise ValueError(
            f"resume environment differs: {changed}; cannot mix measurements"
        )
