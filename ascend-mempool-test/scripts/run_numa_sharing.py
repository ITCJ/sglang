"""Autonomous two-host/two-NPU independent versus shared BM experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from run_ticket10 import connect, receive_log, send_log, write_json
from ticket10_processes import Peer, Processes

from ascend_mempool.sharing import SharingPlan, group_sample, port_plan
from ascend_mempool.sharing_cases import cases
from ascend_mempool.sharing_control import CohortServer

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--prefill-host", required=True)
    parser.add_argument("--decode-host", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--devices", type=int, nargs=2, default=[0, 1])
    parser.add_argument("--numa-node", type=int, required=True)
    parser.add_argument("--prompt-tokens", type=int, default=8192)
    parser.add_argument("--decode-tokens", type=int, default=16384)
    parser.add_argument("--topk", type=int, default=2048)
    parser.add_argument("--block-dims", type=int, nargs="+", default=[24, 48])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=64)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--delay-matmuls", type=int, default=512)
    parser.add_argument("--control-port", type=int, default=19400)
    parser.add_argument("--timeout", type=float, default=3600)
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument(
        "--diagnose-write-read",
        action="store_true",
        help="Only independent write_then_read 1/16-request cases; never acceptance",
    )
    diagnostics.add_argument(
        "--diagnose-independent",
        action="store_true",
        help="One complete independent matrix with stack diagnostics; never acceptance",
    )
    diagnostics.add_argument(
        "--diagnose-publication",
        action="store_true",
        help="Only delayed publication and traces in both pool modes; never acceptance",
    )
    parser.add_argument(
        "--log-root", type=Path, default=Path("/home/cryang/log/numa-sharing")
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Eager-only reduced matrix; never full acceptance",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print configuration without NPU imports or network",
    )
    args = parser.parse_args(argv)
    args.diagnostic = (
        args.diagnose_write_read
        or args.diagnose_independent
        or args.diagnose_publication
    )
    if args.diagnostic and args.smoke:
        parser.error("diagnostic cannot be combined with smoke")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.run_id):
        parser.error("run-id must be a simple directory name, at most 80 characters")
    if args.prefill_host == args.decode_host:
        parser.error("two different P/D hosts are required")
    if min(args.devices) < 0 or len(set(args.devices)) != 2:
        parser.error("--devices must name two different local NPU IDs")
    if not 0 <= args.numa_node <= 126:
        parser.error("--numa-node must be in [0, 126]")
    try:
        port_plan(args.control_port, "shared", 1, 1)
    except ValueError as error:
        parser.error(str(error))
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    if (
        min(args.rounds, args.samples, args.repeats, args.warmup, args.delay_matmuls)
        < 1
    ):
        parser.error(
            "rounds, samples, repeats, warmup and delay-matmuls must be positive"
        )
    if not (args.smoke or args.diagnostic) and (args.rounds < 3 or args.samples < 3):
        parser.error("full comparison needs at least three rounds and three samples")
    if args.topk != 2048 or min(args.prompt_tokens, args.decode_tokens) < 8192:
        parser.error("this matrix requires topk=2048 and P/D capacities >=8192")
    if (
        not args.block_dims
        or any(b <= 0 for b in args.block_dims)
        or len(set(args.block_dims)) != len(args.block_dims)
    ):
        parser.error("block-dims must contain distinct positive core counts")
    try:
        SharingPlan(args.prompt_tokens, args.decode_tokens, slots=16).layout()
    except ValueError as error:
        parser.error(str(error))
    return args


def describe(args: argparse.Namespace) -> dict[str, Any]:
    plan = SharingPlan(args.prompt_tokens, args.decode_tokens, slots=16)
    return dict(
        scope="publication_diagnostic"
        if args.diagnose_publication
        else "write_read_diagnostic"
        if args.diagnose_write_read
        else "independent_diagnostic"
        if args.diagnose_independent
        else "eager_smoke"
        if args.smoke
        else "full_component",
        plan=asdict(plan),
        topk=args.topk,
        block_dims=args.block_dims,
        engines=["eager"] if args.smoke else ["eager", "graph"],
        consumer_sides=[0, 1],
        consumer_schedule="one side at a time; opposite side joined and idle",
        mode_order=[["independent", "shared"]]
        if args.diagnose_publication
        else [["independent"]]
        if args.diagnostic
        else [
            ["independent", "shared"] if r % 2 == 0 else ["shared", "independent"]
            for r in range(args.rounds)
        ],
        independent_contributions=plan.contributions("independent"),
        shared_contributions=plan.contributions("shared"),
        rank_stride_bytes=plan.layout().rank_stride_bytes,
        cases=[]
        if args.diagnose_publication
        else [case.signature() for case in cases(args.smoke, args.diagnose_write_read)],
        samples=args.samples,
        repeats=args.repeats,
        warmup=args.warmup,
        delay_matmuls=args.delay_matmuls,
        manual_acceptance="user reviews NUMA/membership evidence, traces and bandwidth before closing ticket01",
    )


def code_identity() -> dict[str, str]:
    digest = hashlib.sha256()
    roots = (
        REPO / "ascend-mempool-test",
        REPO / "python/sglang/srt/hardware_backend/npu",
    )
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            digest.update(str(path.relative_to(REPO)).encode())
            digest.update(path.read_bytes())
    return dict(
        git_sha=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        source_sha256=digest.hexdigest(),
    )


def local_groups(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(reports) != 2 or any(
        r.get("status")
        not in ("checks_passed", "checks_passed_numa_unverified", "smoke_passed")
        or not r.get("closed")
        for r in reports
    ):
        raise RuntimeError(
            "both local workers must complete checks and close their pools"
        )
    config = reports[0]["config"]
    contract = ("mode", "round", "side", "smoke", "block_dims", "samples", "repeats")
    if any(
        r["config"].get(field, False) != config.get(field, False)
        for r in reports
        for field in ("diagnostic", "diagnose_write_read", "diagnose_publication")
    ):
        raise RuntimeError("local workers disagree on diagnostic scope")
    if {r["config"]["member"] for r in reports} != {0, 1} or any(
        any(r["config"][name] != config[name] for name in contract) for r in reports
    ):
        raise RuntimeError(
            "local worker configurations do not describe one matched pair"
        )
    if config.get("diagnose_publication", False):
        for report in reports:
            rows = report.get("delayed_publication", [])
            expected_rows = {
                (engine, variant) for engine in ("eager", "graph") for variant in (0, 1)
            }
            if (
                report.get("samples") != []
                or len(rows) != 4
                or {(r["engine"], r["variant"]) for r in rows} != expected_rows
                or any(r.get("publication_readback_passed") is not True for r in rows)
            ):
                raise RuntimeError("incomplete publication diagnostic report")
            for row in rows:
                if row["engine"] != "eager":
                    continue
                selected, writer, gap = (
                    row[name]
                    for name in ("selected_ms", "writer_ms", "writer_minus_selected_ms")
                )
                if (
                    not all(math.isfinite(v) for v in (selected, writer, gap))
                    or min(selected, writer) < 0
                    or not math.isclose(gap, writer - selected)
                    or (
                        report["config"]["member"] == 1
                        and (gap <= 0 or row["selected_before_writer_done"] is not True)
                    )
                ):
                    raise RuntimeError("invalid publication diagnostic timing")
        return []
    expected = {
        json.dumps([case.signature(), engine, blocks, sample], sort_keys=True)
        for case in cases(config["smoke"], config.get("diagnose_write_read", False))
        for engine in (["eager"] if config["smoke"] else ["eager", "graph"])
        for blocks in config["block_dims"]
        for sample in range(config["samples"])
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        seen = set()
        for row in report["samples"]:
            key = json.dumps(
                [row["case"], row["engine"], row["block_dim"], row["sample"]],
                sort_keys=True,
            )
            if key in seen:
                raise RuntimeError("duplicate benchmark sample")
            seen.add(key)
            if (
                any(
                    row[name] != config[name]
                    for name in ("side", "mode", "round", "repeats")
                )
                or row["member"] != report["config"]["member"]
                or row.get("consumer_side") != config["side"]
                or row.get("opposite_side_idle") is not True
            ):
                raise RuntimeError(
                    "sample does not isolate the requested consumer pair"
                )
            for field in ("device_calls_ms", "selected_calls_ms"):
                if len(row[field]) != config["repeats"] or any(
                    not math.isfinite(value) or value <= 0 for value in row[field]
                ):
                    raise RuntimeError("missing or invalid per-call device timings")
            if not math.isclose(row["device_ms"], sum(row["device_calls_ms"])):
                raise RuntimeError(
                    "device batch duration differs from its call timings"
                )
            groups.setdefault(key, []).append(row)
        if seen != expected:
            raise RuntimeError(
                "worker report is missing part of the requested benchmark matrix"
            )
    result = []
    for rows in groups.values():
        rows.sort(key=lambda row: row["member"])
        sample = group_sample(rows)
        first = rows[0]
        result.append(
            dict(
                case=first["case"],
                engine=first["engine"],
                block_dim=first["block_dim"],
                mode=first["mode"],
                round=first["round"],
                side=first["side"],
                sample=first["sample"],
                repeats=first["repeats"],
                concurrent_accessors=sum(r["copy_bytes"] > 0 for r in rows),
                per_rank=rows,
                **sample,
            )
        )
    return result


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def summarize(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in groups:
        key = json.dumps(
            [row["side"], row["case"], row["engine"], row["block_dim"]], sort_keys=True
        )
        buckets.setdefault(key, []).append(row)
    output = []
    for rows in buckets.values():
        item = {name: rows[0][name] for name in ("side", "case", "engine", "block_dim")}
        for mode in ("independent", "shared"):
            selected = [r for r in rows if r["mode"] == mode]
            valid = [
                r
                for r in selected
                if r["concurrent_accessors"] < 2 or r["aligned_batches"]
            ]
            if not valid or len({r["round"] for r in valid}) != len(
                {r["round"] for r in selected}
            ):
                raise RuntimeError(
                    f"no aligned samples in a round for {item}, {mode}; inspect launch skew or increase --repeats"
                )
            times = [r["host_envelope_ms"] / r["repeats"] for r in valid]
            item[mode] = dict(
                samples=len(valid),
                excluded_unaligned=len(selected) - len(valid),
                group_ms_p50=percentile(times, 0.5),
                group_ms_p95=percentile(times, 0.95),
                launch_skew_ms_p95=percentile(
                    [r["launch_skew_ms"] for r in valid], 0.95
                ),
                per_rank_device_ms=[
                    dict(
                        p50=percentile(
                            [r["device_ms"][m] / r["repeats"] for r in valid], 0.5
                        ),
                        p95=percentile(
                            [r["device_ms"][m] / r["repeats"] for r in valid], 0.95
                        ),
                    )
                    for m in (0, 1)
                ],
                per_rank_selected_ms=[
                    dict(
                        p50=percentile(
                            [
                                t
                                for r in valid
                                for t in r["per_rank"][m]["selected_calls_ms"]
                            ],
                            0.5,
                        ),
                        p95=percentile(
                            [
                                t
                                for r in valid
                                for t in r["per_rank"][m]["selected_calls_ms"]
                            ],
                            0.95,
                        ),
                    )
                    for m in (0, 1)
                ],
                copy_payload_gbps_p50=percentile(
                    [r["copy_payload_gbps"] for r in valid], 0.5
                )
                if valid[0]["copy_bytes"]
                else None,
                bm_payload_gbps_p50=percentile(
                    [r["bm_payload_gbps"] for r in valid], 0.5
                )
                if valid[0]["bm_copy_bytes"]
                else None,
                logical_gbps_p50=percentile([r["logical_gbps"] for r in valid], 0.5)
                if valid[0]["logical_bytes"]
                else None,
            )
        item["shared_over_independent_group_p50"] = (
            item["shared"]["group_ms_p50"] / item["independent"]["group_ms_p50"]
        )
        output.append(item)
    add_reader_efficiency(output)
    return output


def add_reader_efficiency(comparison: list[dict[str, Any]]) -> None:
    """Compare measured dual throughput to two separately measured single readers."""
    matched: dict[str, dict[int, dict[str, Any]]] = {}
    for item in comparison:
        case = item["case"]
        if case["operation"] != "read":
            continue
        workload = {
            key: value for key, value in case.items() if key not in ("name", "accessor")
        }
        key = json.dumps(
            [item["side"], item["engine"], item["block_dim"], workload], sort_keys=True
        )
        matched.setdefault(key, {})[case["accessor"]] = item
    for members in matched.values():
        if set(members) != {-1, 0, 1}:
            continue
        for mode in ("independent", "shared"):
            dual = members[-1][mode]["bm_payload_gbps_p50"]
            singles = [
                members[member][mode]["bm_payload_gbps_p50"] for member in (0, 1)
            ]
            members[-1][mode]["dual_reader_efficiency"] = dict(
                fraction=dual / sum(singles),
                dual_measured_bm_gbps=dual,
                single_measured_bm_gbps=singles,
                formula="dual measured throughput / (single owner + single follower measured throughput)",
            )


def completion_outcome(
    smoke: bool,
    local_numa_verified: bool,
    peer_numa_verified: bool,
    *,
    diagnostic: bool = False,
) -> dict[str, Any]:
    """Both hosts retain incomplete acceptance while still returning measurements."""
    verified = local_numa_verified and peer_numa_verified
    return dict(
        status="diagnostic_passed"
        if diagnostic
        else "smoke_passed"
        if smoke
        else "component_checks_passed"
        if verified
        else "component_results_pending_numa",
        exit_code=0 if diagnostic or smoke or verified else 2,
        numa_placement_verified=verified,
        numa_acceptance="verified" if verified else "pending",
    )


def execute(args: argparse.Namespace, peer: Peer, root: Path) -> dict[str, Any]:
    plan = describe(args)
    identity = dict(run_id=args.run_id, plan=plan, code=code_identity())
    peer.send("identity", value=identity)
    if peer.expect("identity")["value"] != identity:
        raise RuntimeError("P/D plan, code fingerprint or run ID differs")
    write_json(root / "identity.json", identity)
    all_groups: list[dict[str, Any]] = []
    allocations: list[dict[str, Any]] = []
    publication_reports: list[dict[str, Any]] = []
    for round_id, order in enumerate(plan["mode_order"]):
        for mode in order:
            phase = f"round-{round_id}/{mode}"
            stage = root / phase
            stage.mkdir(parents=True)
            server = None
            try:
                if args.role == "prefill":
                    server = CohortServer(
                        args.prefill_host,
                        args.control_port + 1,
                        f"{args.run_id}/{phase}",
                        args.timeout,
                    )
                peer.barrier(phase + "/coordinator")
                with Processes(stage) as processes:
                    children = []
                    for member, device in enumerate(args.devices):
                        config = dict(
                            plan=plan["plan"],
                            mode=mode,
                            side=0 if args.role == "prefill" else 1,
                            member=member,
                            device_id=device,
                            numa_node=args.numa_node,
                            prefill_host=args.prefill_host,
                            decode_host=args.decode_host,
                            control_port=args.control_port,
                            timeout=args.timeout,
                            identity=f"{args.run_id}/{phase}",
                            round=round_id,
                            topk=args.topk,
                            block_dims=args.block_dims,
                            smoke=args.smoke,
                            diagnose_write_read=args.diagnose_write_read,
                            diagnostic=args.diagnostic,
                            diagnose_publication=args.diagnose_publication,
                            warmup=args.warmup,
                            samples=args.samples,
                            repeats=args.repeats,
                            delay_matmuls=args.delay_matmuls,
                        )
                        config_path = stage / f"member-{member}.json"
                        write_json(config_path, config)
                        children.append(
                            processes.start(
                                f"member-{member}",
                                [
                                    sys.executable,
                                    "-u",
                                    str(SCRIPTS / "sharing_worker.py"),
                                    "--config",
                                    str(config_path),
                                    "--output",
                                    str(stage / f"member-{member}"),
                                ],
                            )
                        )

                    def check_children() -> None:
                        peer.check()
                        for child in children:
                            if child.poll() not in (None, 0):
                                raise RuntimeError(
                                    f"worker {child.pid} failed with {child.returncode}; see {stage}"
                                )

                    for member, child in enumerate(children):
                        processes.wait(
                            f"member-{member}",
                            child,
                            timeout=args.timeout,
                            check=check_children,
                        )
                reports = [
                    json.loads((stage / f"member-{m}/report.json").read_text())
                    for m in (0, 1)
                ]
                groups = local_groups(reports)
                all_groups.extend(groups)
                allocations.extend(r["mapping"] for r in reports)
                publication_reports.extend(
                    dict(
                        mode=mode,
                        round=round_id,
                        member=r["config"]["member"],
                        checks=r["delayed_publication"],
                    )
                    for r in reports
                    if "delayed_publication" in r
                )
                write_json(stage / "groups.json", groups)
                peer.barrier(phase + "/closed")
                print(f"PASS {phase}", flush=True)
            finally:
                if server is not None:
                    server.close()
    owners = [r for r in allocations if r["local_dram_bytes"] > 0]
    if not owners:
        raise RuntimeError("no physical owner allocation reports")
    local_numa_verified = all(r["numa"]["placement_verified"] for r in owners)
    peer.send("numa-result", placement_verified=local_numa_verified)
    peer_numa_verified = peer.expect("numa-result")["placement_verified"]
    if type(peer_numa_verified) is not bool:
        raise RuntimeError("invalid peer NUMA result")
    result = dict(
        **completion_outcome(
            args.smoke,
            local_numa_verified,
            peer_numa_verified,
            diagnostic=args.diagnostic,
        ),
        scope=plan["scope"],
        role=args.role,
        identity=identity,
        physical_allocations=allocations,
        delayed_publication=publication_reports,
        groups=all_groups,
        comparison=[] if args.diagnostic else summarize(all_groups),
        service_performance_acceptance="not run",
        user_acceptance="pending",
        ticket_state="open",
    )
    write_json(root / "summary.json", result)
    if args.role == "decode":
        send_log(peer, root / "summary.json")
    else:
        receive_log(peer, root / "decode-summary.json")
    peer.barrier("reports-collected")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.describe:
        print(json.dumps(describe(args), indent=2))
        return 0
    if not args.log_root.resolve().is_relative_to(Path("/home/cryang/log")):
        raise ValueError("NPU results must be saved below /home/cryang/log/")
    root = args.log_root / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "command.json", sys.argv if argv is None else list(argv))

    def stop(signum: int, frame: Any) -> None:
        raise RuntimeError(f"runner received signal {signum}")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        with Peer(connect(args), args.timeout) as peer:
            try:
                result = execute(args, peer, root)
            except BaseException as exc:
                try:
                    peer.send("error", message=str(exc))
                except OSError:
                    pass
                raise
        print(f"{result['status'].upper()}: {root / 'summary.json'}", flush=True)
        return int(result["exit_code"])
    except BaseException as exc:
        write_json(
            root / "failure.json",
            dict(status="failed", error=f"{type(exc).__name__}: {exc}"),
        )
        print(f"FAILED: {exc}; logs: {root}", file=sys.stderr, flush=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
