"""One paired target startup with synchronized index_add_ warmup diagnostics.

Use the failed target's effective-config.json. No component/reference rerun,
requests or acceptance results. Existing process ownership/cleanup is reused.
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
    service_environment,
    service_ports,
    verify_mempool_session,
    write_json,
)
from run_ticket10 import connect, wait_http
from ticket10_processes import Peer, Processes


def check_warmups(log: Path) -> list[dict]:
    completed = []
    for line in log.read_text(errors="replace").splitlines():
        if "[MEMPOOL_INDEX_DIAG] " in line:
            item = json.loads(line.split("[MEMPOOL_INDEX_DIAG] ", 1)[1])
            if item["phase"] == "warmup_complete":
                completed.append(item)
    expected = {(device, warmup) for device in range(16) for warmup in range(2)}
    actual = {(item["device"], item["warmup"]) for item in completed if item["calls"]}
    if actual != expected:
        raise ValueError(f"missing TP16 warmup diagnosis: {sorted(expected - actual)}")
    return completed


def startup(runner: Runner, library: Path) -> None:
    root, args, config, peer = runner.root, runner.args, runner.config, runner.peer
    env = service_environment(config, args, root, target=True)
    env["SGLANG_UNIDEX_MULTIBASE_LIBRARY"] = str(library)
    env["SGLANG_NPU_MEMPOOL_INDEX_DIAG"] = "0" if runner.is_p else "1"
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
        if not runner.is_p:
            runner.report["warmups"] = check_warmups(root / "launcher.log")
        peer.send("diagnostic-ready")
        peer.expect("diagnostic-ready", check=watch)
        if runner.is_p:
            wait_http(
                f"http://{config.prefill_host}:{config.main_port}/ready",
                config.startup_timeout,
                watch,
            )
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
    runner.report.update(status="diagnostic_startup_passed", cleanup="passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if (
        not args.run_id
        or Path(args.run_id).name != args.run_id
        or args.run_id in (".", "..")
    ):
        parser.error("run-id must be a new directory name")
    config = RouterConfig.read(str(args.config))
    if not config.observe:
        parser.error("use the observed target's effective-config.json")
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
    library_hash = hashlib.sha256(library.read_bytes()).hexdigest()

    def stop(signum, frame):
        raise KeyboardInterrupt(f"diagnosis interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with Peer(connect(args), args.timeout) as peer:
        runner = Runner(args, config, peer, root)
        runner.report.update(
            mode="target-startup-diagnosis",
            coverage="startup only; synchronized index_add_ in eager warmup; no requests",
            target_acceptance="not-run",
            source_config=str(source_config),
            library_sha256=library_hash,
            index_diagnosis=args.role == "decode",
            skip_http_warmup=True,
        )
        try:
            runner.preflight()
            startup(runner, library)
            print("TARGET_STARTUP_DIAGNOSED; not an acceptance result", flush=True)
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
