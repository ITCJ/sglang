"""One paired startup, optionally collecting a saved request and device trace.

Startup-only remains the default. Sampling reuses owned-process cleanup and
never runs component/reference acceptance or marks full acceptance passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import time
from pathlib import Path

from run_mempool_min import (
    REPO,
    SCRIPTS,
    RouterConfig,
    Runner,
    request_json,
    service_environment,
    service_ports,
    verify_mempool_session,
    write_json,
)
from run_ticket10 import connect, wait_http
from ticket10_processes import Peer, Processes


def read_warmups(log: Path) -> dict:
    completed, failures, parse_errors = [], [], []
    decoder = json.JSONDecoder()
    for number, line in enumerate(log.read_text(errors="replace").splitlines(), 1):
        # Workers share stdout. A newline may arrive after another rank's JSON
        # or ordinary log text; decode each tagged object, not the whole line.
        for chunk in line.split("[MEMPOOL_INDEX_DIAG] ")[1:]:
            try:
                item, _ = decoder.raw_decode(chunk.lstrip())
            except json.JSONDecodeError as error:
                parse_errors.append(
                    dict(line=number, error=str(error), text=chunk[:500])
                )
                continue
            if item["phase"] == "warmup_complete":
                completed.append(item)
            elif item["phase"] in ("failed", "warmup_failed"):
                failures.append(dict(line=number, **item))
    expected = {(device, warmup) for device in range(16) for warmup in range(2)}
    actual = {(item["device"], item["warmup"]) for item in completed if item["calls"]}
    return dict(
        status="warmup_evidence_complete"
        if actual == expected and not failures and not parse_errors
        else "warmup_evidence_incomplete",
        acceptance="not-evaluated",
        coverage="saved eager warmup records only; no startup/cleanup/Graph acceptance",
        source=str(log.resolve()),
        completed=completed,
        missing=sorted(expected - actual),
        failures=failures,
        parse_errors=parse_errors,
    )


def check_warmups(log: Path) -> list[dict]:
    report = read_warmups(log)
    write_json(log.parent / "index-diagnosis.json", report)
    if report["status"] != "warmup_evidence_complete":
        raise ValueError(
            f"missing TP16 warmup diagnosis or failed/damaged records; see {log.parent / 'index-diagnosis.json'}"
        )
    return report["completed"]


def sample_request(runner: Runner, watch) -> None:
    """One saved request, then export the existing paired observation trace."""
    root, config, peer = runner.root, runner.config, runner.peer
    payload = json.loads(runner.args.sample_json.read_text())
    peer.send("sample-input", payload=payload)
    if peer.expect("sample-input")["payload"] != payload:
        raise ValueError("P/D sampling inputs differ")
    write_json(root / "sample-input.json", payload)
    base = f"http://{config.prefill_host}:{config.main_port}"
    if runner.is_p:
        watch()
        result = request_json(
            base + "/generate", payload=payload, timeout=config.operation_timeout
        )
        write_json(root / "sample-response.json", result)
        if (
            len(result.get("output_ids", []))
            != payload["sampling_params"]["max_new_tokens"]
        ):
            raise ValueError(
                "sample response did not complete the requested output length"
            )
        peer.send("sample-response", result=result)
        request_json(base + "/observation/flush", payload={})
        deadline = time.monotonic() + config.operation_timeout
        while True:
            watch()
            status = request_json(base + "/status")
            if status.get("fault"):
                raise RuntimeError(status["fault"])
            if status.get("observation_flushed") == ["decode", "prefill"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("paired observation export did not finish")
            time.sleep(0.2)
    else:
        result = peer.expect("sample-response", check=watch)["result"]
        write_json(root / "sample-response.json", result)
    peer.send("sample-exported")
    peer.expect("sample-exported", check=watch)
    traces = sorted(root.rglob("trace_view.json"))
    # Existing trace layout is one directory per TP rank, on each host.
    missing = [
        rank
        for rank in range(16)
        if not any(
            (root / "observe" / f"trace-{runner.args.role}-{rank}").rglob(
                "trace_view.json"
            )
        )
    ]
    write_json(
        root / "sample-traces.json",
        dict(files=[str(p.relative_to(root)) for p in traces], missing_ranks=missing),
    )
    if missing:
        raise ValueError(f"sample trace export missing ranks: {missing}")
    runner.report["sample"] = dict(
        requests=1, output_tokens=len(result["output_ids"]), traces=len(traces)
    )


def startup(runner: Runner, library: Path | None) -> None:
    root, args, config, peer = runner.root, runner.args, runner.config, runner.peer
    env = service_environment(config, args, root, target=True)
    if library is not None:
        env["SGLANG_UNIDEX_MULTIBASE_LIBRARY"] = str(library)
    else:
        env.pop("SGLANG_UNIDEX_MULTIBASE_LIBRARY", None)
    index_sync = not getattr(args, "without_index_sync", False)
    env["SGLANG_NPU_MEMPOOL_INDEX_DIAG"] = (
        "1" if not runner.is_p and index_sync else "0"
    )
    # Graph construction still runs; suppress the later HTTP dummy request.
    env["SKIP_SERVER_WARMUP"] = "1"
    peer.barrier("diagnostic-start")
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
                    args.run_id,
                ],
            )
        server = jobs.start(
            "launcher", ["bash", str(SCRIPTS / "run_service.sh"), args.role, "formal"]
        )

        def watch():
            jobs.alive("NPU service", server)
            if router is not None:
                jobs.alive("Router", router)
            peer.check()

        wait_http(config.backend(args.role) + "/ready", config.startup_timeout, watch)
        if not runner.is_p and index_sync:
            runner.report["warmups"] = check_warmups(root / "launcher.log")
        peer.send("diagnostic-ready")
        peer.expect("diagnostic-ready", check=watch)
        if runner.is_p:
            wait_http(
                f"http://{config.prefill_host}:{config.main_port}/ready",
                config.startup_timeout,
                watch,
            )
        if getattr(args, "sample_json", None):
            sample_request(runner, watch)
        peer.send("diagnostic-finished")
        peer.expect("diagnostic-finished", check=watch)
    peer.barrier("diagnostic-stopped")
    deadline = time.monotonic() + config.operation_timeout
    for port in sorted(service_ports(config, args.role)):
        verify_mempool_session.released_port(
            runner.host,
            port,
            timeout=max(0, deadline - time.monotonic()),
            evidence_path=root / f"port-release-{port}.json",
            check=peer.check,
        )
    peer.barrier("diagnostic-ports-released")
    runner.report.update(
        status="sample_collected"
        if getattr(args, "sample_json", None)
        else "diagnostic_startup_passed",
        cleanup="passed",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--sample-json",
        type=Path,
        help="one saved request followed by paired trace export; not acceptance",
    )
    parser.add_argument(
        "--legacy-kernel",
        action="store_true",
        help="sampling an isolated pre-migration checkout without a multibase plugin",
    )
    parser.add_argument(
        "--without-index-sync",
        action="store_true",
        help="start target with the normal asynchronous path; no dispatch diagnostics",
    )
    parser.add_argument(
        "--read-log", type=Path, help="read saved evidence only; never start jobs"
    )
    args = parser.parse_args()
    if args.read_log is not None:
        report = read_warmups(args.read_log)
        output = args.read_log.parent / "index-diagnosis.json"
        write_json(output, report)
        print(
            json.dumps(
                dict(
                    status=report["status"],
                    completed_pairs=len(
                        {
                            (r["device"], r["warmup"])
                            for r in report["completed"]
                            if r["calls"]
                        }
                    ),
                    calls_per_warmup=sorted({r["calls"] for r in report["completed"]}),
                    missing=report["missing"],
                    failures=report["failures"],
                    parse_errors=report["parse_errors"],
                    acceptance="not-evaluated",
                    report=str(output),
                )
            ),
            flush=True,
        )
        return 0 if report["status"] == "warmup_evidence_complete" else 1
    if not args.role or args.config is None or not args.run_id:
        parser.error(
            "startup requires --role, --config and --run-id; or use --read-log"
        )
    if (
        not args.run_id
        or Path(args.run_id).name != args.run_id
        or args.run_id in (".", "..")
    ):
        parser.error("run-id must be a new directory name")
    config = RouterConfig.read(str(args.config))
    if not config.observe:
        parser.error("use the observed target's effective-config.json")
    if args.sample_json and (not args.without_index_sync or not config.observe_trace):
        parser.error("sample requires --without-index-sync and observe_trace=true")
    root = Path("/home/cryang/log/k2") / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    source_config = args.config.resolve()
    args.config = root / "effective-config.json"
    args.config.write_bytes(source_config.read_bytes())
    # Reuse the target-only preflight without enabling any acceptance stage.
    args.mode = "serve"
    args.prefill_host = config.prefill_host
    args.control_port = config.runner_port
    args.timeout = config.startup_timeout
    library = (
        REPO
        / "python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/build/libunidex_multibase.so"
    )
    if args.legacy_kernel:
        library = None
    library_hash = hashlib.sha256(library.read_bytes()).hexdigest() if library else None

    def stop(signum, frame):
        raise KeyboardInterrupt(f"diagnosis interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with Peer(connect(args), args.timeout) as peer:
        runner = Runner(args, config, peer, root)
        runner.report.update(
            mode="target-startup-diagnosis",
            coverage="startup only; normal asynchronous warmup; no requests"
            if args.without_index_sync
            else "startup only; synchronized index_add_ in eager warmup; no requests",
            target_acceptance="not-run",
            source_config=str(source_config),
            library_sha256=library_hash,
            index_diagnosis=args.role == "decode" and not args.without_index_sync,
            skip_http_warmup=True,
        )
        if args.sample_json:
            runner.report.update(
                mode="migration-short-sample",
                coverage="one saved request with trace; capture/warmup also recorded; not full acceptance or compute-overlap proof",
                sample_input_sha256=hashlib.sha256(
                    args.sample_json.read_bytes()
                ).hexdigest(),
            )
        try:
            runner.preflight()
            startup(runner, library)
            print(
                "MIGRATION_SAMPLE_COLLECTED; stopped for user cleanup confirmation"
                if args.sample_json
                else "TARGET_STARTUP_DIAGNOSED; not an acceptance result",
                flush=True,
            )
            return 0
        except (Exception, KeyboardInterrupt) as error:
            runner.report.update(status="failed", error=repr(error))
            try:
                peer.send("error", message=str(error))
            except OSError:
                pass
            print(f"{type(error).__name__}: {error}", flush=True)
            return 1
        finally:
            write_json(root / "summary.json", runner.report)


if __name__ == "__main__":
    raise SystemExit(main())
