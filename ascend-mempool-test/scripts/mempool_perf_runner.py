"""Restart-isolated ABCE performance rounds on the existing paired runner."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import mempool_logical_cases
from mempool_perf_outputs import OUTPUT_POLICY, compare_outputs
from mempool_perf_report import CASES, assess_sample, generate_report, write_json
from run_mempool_min import REPO, RUN_ENV, SCRIPTS, request_json, service_environment
from run_ticket10 import receive_log, send_log, wait_http
from ticket10_processes import Processes


def topology(root: Path, interface: str) -> None:
    """Keep failed/unsupported probes explicitly; never infer transport support."""
    commands = [
        ["npu-smi", "info"],
        ["npu-smi", "info", "-t", "topo"],
        ["numactl", "--hardware"],
        ["lscpu"],
        ["ip", "-j", "address", "show", "dev", interface],
        ["ip", "-d", "link", "show", "dev", interface],
        ["rdma", "link", "show"],
        ["hccn_tool", "-i", "0", "-ip", "-g"],
    ]
    probes = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            probes.append(
                dict(
                    command=command, returncode=result.returncode, output=result.stdout
                )
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            probes.append(dict(command=command, status="unavailable", error=str(exc)))
    versions = {}
    for pattern in (
        "/usr/local/Ascend/driver/version.info",
        "/usr/local/Ascend/ascend-toolkit/latest/version.cfg",
        "/usr/local/Ascend/ascend-toolkit/latest/*-linux/ascend_toolkit_install.info",
    ):
        import glob

        for name in glob.glob(pattern):
            versions[name] = Path(name).read_text(errors="replace")
    controls = {
        key: os.environ.get(key, "unset; launch/runtime default")
        for key in (
            "SGLANG_ENABLE_DETERMINISTIC_INFERENCE",
            "SGLANG_FORCE_STREAM_INTERVAL",
            "HCCL_DETERMINISTIC",
            "LCCL_DETERMINISTIC",
            "ASCEND_LAUNCH_BLOCKING",
        )
    }
    copy_files = [
        REPO / "python/sglang/srt/hardware_backend/npu/mempool" / name
        for name in ("copy.py", "offload.py", "prefix.py", "runtime.py")
    ]
    write_json(
        root / "topology.json",
        dict(
            numa_nodes={
                p.name: {
                    name: (p / name).read_text().strip()
                    for name in ("cpulist", "distance")
                    if (p / name).exists()
                }
                for p in Path("/sys/devices/system/node").glob("node[0-9]*")
            },
            platform="Ascend NPU",
            host=os.uname().nodename,
            interface=interface,
            commands=probes,
            versions=versions,
            controls=controls,
            copy_source_sha256={
                str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in copy_files
            },
            explicit_rank_numa_map="unsupported by stage0 require_rank_pair_startup",
            physical_numa_placement="unverified; registration is requested placement only",
            device_direct_rdma="unknown; raw link/transport evidence retained, not inferred from NIC existence",
            cuda_gpudirect="not applicable",
        ),
    )


def run_round(runner: Any, number: int) -> None:
    args, config, peer = runner.args, runner.config, runner.peer
    name = f"round-{number:02d}"
    root = runner.root / name
    env = service_environment(config, args, root, target=True)
    peer.barrier(name + "-start")
    outcome: dict[str, Any] = dict(
        round=number, warmup=number == 1, samples=[], paired_cleanup=False
    )
    with Processes(root, env) as jobs:
        router = None
        if runner.is_p:
            router = jobs.start(
                "router",
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "sglang.srt.disaggregation.ascend.mempool_router",
                    "--config",
                    str(args.config),
                    "--run-id",
                    f"{args.run_id}-r{number:02d}",
                ],
            )
        # Worker run identity must match this fresh Main, on both hosts.
        env[RUN_ENV] = f"{args.run_id}-r{number:02d}"
        server = jobs.start(
            "launcher", ["bash", str(SCRIPTS / "run_service.sh"), args.role, "formal"]
        )

        def watch() -> None:
            jobs.alive("NPU service", server)
            if router is not None:
                jobs.alive("Router", router)
            peer.check()

        wait_http(config.backend(args.role) + "/ready", config.startup_timeout, watch)
        peer.barrier(name + "-backends-up")
        base = f"http://{config.prefill_host}:{config.main_port}"
        if runner.is_p:
            wait_http(base + "/ready", config.startup_timeout, watch)
        peer.barrier(name + "-ready")
        if runner.is_p:
            outcome["initial"] = request_json(base + "/status")
            write_json(root / "outcome.json", outcome)
            previous: dict[str, Any] = {}
            round_error = ""
            if (
                outcome["initial"]["directory"]
                or outcome["initial"]["ledger"]["p_prefix"]["outstanding"]
            ):
                round_error = "prefix did not reset to empty after service restart"
            for index, case in enumerate(CASES):
                record: dict[str, Any] = dict(case=case, rid="", valid=False)
                outcome["samples"].append(record)
                try:
                    watch()
                    fixture = runner.cases[index]
                    record["result"] = request_json(
                        base + "/generate",
                        payload={
                            k: fixture[k] for k in ("input_ids", "sampling_params")
                        },
                        timeout=config.operation_timeout,
                    )
                    record["output_diagnostics"] = compare_outputs(
                        fixture, runner.reference[index], record["result"]
                    )
                    record["rid"] = record["result"].get("meta_info", {}).get("id", "")
                    # Archive before assertions, including malformed responses.
                    write_json(root / "outcome.json", outcome)
                    record["snapshot"] = request_json(base + "/status", timeout=30)
                    if record["snapshot"]["completed"]:
                        record["rid"] = record["snapshot"]["completed"][-1]["rid"]
                    write_json(root / "outcome.json", outcome)
                    assessed = assess_sample(
                        case,
                        fixture,
                        runner.reference[index],
                        record["result"],
                        record["snapshot"],
                        previous,
                    )
                    if assessed["rid"]:
                        record["rid"] = assessed["rid"]
                    record.update(
                        valid=assessed["valid"],
                        errors=assessed["errors"],
                        output_diagnostics=assessed["output_diagnostics"],
                    )
                    if round_error:
                        record["errors"].append(round_error)
                        record["valid"] = False
                    previous = record["snapshot"]["directory"]
                except Exception as exc:
                    record["execution_error"] = f"{type(exc).__name__}: {exc}"
                    record["errors"] = [record["execution_error"]]
                    record["valid"] = False
                if not record["valid"]:
                    record["error"] = "; ".join(record["errors"])
                    print(
                        f"PERF_INVALID round={number} case={case} {record['error']}",
                        flush=True,
                    )
                difference = record.get("output_diagnostics", {}).get("ids", {})
                if difference.get("equal") is False:
                    print(
                        f"PERF_OUTPUT_DIFFERENCE round={number} case={case} "
                        f"first={difference['first_difference']} diagnostic_only=true",
                        flush=True,
                    )
                write_json(root / "outcome.json", outcome)
            peer.send("perf-round-done", round=number)
        else:
            message = peer.expect("perf-round-done", check=watch)
            if message != dict(round=number):
                raise ValueError("perf peer round mismatch")
    # Snapshot logs after all local children have exited, not while tee may buffer.
    peer.barrier(name + "-stopped")
    if runner.is_p:
        for filename in (
            "decode.log",
            "decode-environment.txt",
            "decode-model-config.json",
            "decode-quant-model-description.json",
        ):
            receive_log(peer, root / filename)
        outcome["paired_cleanup"] = True
        write_json(root / "outcome.json", outcome)
        send_log(peer, root / "outcome.json")
    else:
        for filename in (
            "decode.log",
            "decode-environment.txt",
            "decode-model-config.json",
            "decode-quant-model-description.json",
        ):
            send_log(peer, root / filename)
        receive_log(peer, root / "outcome.json")
    write_json(root / "process-cleanup.json", dict(status="passed", paired=True))
    peer.barrier(name + "-archived")
    runner.report["stages"].append(name)
    write_json(runner.root / "summary.json", runner.report)


def run(runner: Any) -> None:
    """Reference outside timings, then five independently restarted ABCE rounds."""
    runner.report.update(
        coverage="stage0 serial ABCE perf; not full functional acceptance",
        output_policy=OUTPUT_POLICY,
    )
    topology(runner.root, runner.config.interface)
    if runner.is_p:
        receive_log(runner.peer, runner.root / "decode-topology.json")
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            runner.config.model_path, trust_remote_code=True
        )
        runner.cases = mempool_logical_cases.make_cases(
            tokenizer, runner.config.page_size
        )
        for case in runner.cases:
            # Same input IDs as ticket04; only the requested generation changes.
            case["sampling_params"] = dict(
                temperature=0, top_k=1, top_p=1, max_new_tokens=128, ignore_eos=True
            )
        write_json(runner.root / "cases.json", runner.cases)
    else:
        send_log(runner.peer, runner.root / "topology.json")
    manifest = dict(
        schema=2,
        output_policy=OUTPUT_POLICY,
        output_difference="diagnostic only; continue ABCE and all rounds",
        logical_answer="first JSON object versus independent expected_answer; tail retained",
        acceptance="user review; automated check results are not user acceptance",
        rounds=runner.args.perf_rounds,
        warmup_rounds=[1],
        case_order=list(CASES),
        prefix_reset="restart Main and both TP16 services every round",
        numa_runs=1,
        numa_reason="explicit rank_numa_map rejected by stage0; current eight nodes already assign two private ranks each",
        baseline_commit=runner.report["identity"]["git_sha"],
        metric_clock="monotonic_ns of one process; no cross-host subtraction",
        graph="P eager; D capture batch 16; native warmup unchanged",
        heavy_checks=False,
        faults=False,
        profiler=False,
    )
    write_json(runner.root / "perf-manifest.json", manifest)
    runner.peer.barrier("perf-fixtures-ready")
    runner.stage(target=False)
    for number in range(1, runner.args.perf_rounds + 1):
        run_round(runner, number)
    if runner.is_p:
        report = generate_report(runner.root)
        runner.peer.send(
            "perf-result",
            status=report["status"],
            invalid_samples=report["invalid_samples"],
        )
        for filename in (
            "baseline.md",
            "samples.jsonl",
            "perf-report.json",
            "output-analysis.json",
        ):
            send_log(runner.peer, runner.root / filename)
    else:
        report = runner.peer.expect("perf-result")
        for filename in (
            "baseline.md",
            "samples.jsonl",
            "perf-report.json",
            "output-analysis.json",
        ):
            receive_log(runner.peer, runner.root / filename)
    runner.peer.barrier("perf-report-archived")
    runner.report.update(
        status=report["status"],
        perf_report=str(runner.root / "baseline.md"),
        invalid_samples=report["invalid_samples"],
        hardware_execution="completed by user runner",
        user_accepted=False,
    )
    if report["status"] != "passed":
        raise ValueError(
            "perf run recorded failed checks; inspect baseline.md and output-analysis.json for user review"
        )
