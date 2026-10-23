"""One command per existing NPU container for ticket01 serving or acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(REPO / "ascend-mempool-test/src"))

import msgspec
import verify_mempool_min
import verify_service
from run_ticket10 import (
    available_port,
    connect,
    model_identity,
    receive_log,
    send_log,
    wait_http,
)
from ticket10_processes import Peer, Processes

if TYPE_CHECKING:
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import (
        CONFIG_ENV,
        RUN_ENV,
        RouterConfig,
    )
else:
    from ascend_mempool_pd.mempool_router_protocol import (
        CONFIG_ENV,
        RUN_ENV,
        RouterConfig,
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--mode", choices=("serve", "acceptance"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--log-root", type=Path, default=Path("/home/cryang/log/mempool-min")
    )
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.run_id):
        parser.error("run-id must be a simple new directory name")
    if not args.log_root.resolve().is_relative_to(Path("/home/cryang/log").resolve()):
        parser.error("logs must remain under /home/cryang/log/")
    args.config = args.config.resolve()
    return args


def service_environment(
    config: RouterConfig, args: argparse.Namespace, root: Path, *, target: bool
) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        CONFIG_ENV,
        RUN_ENV,
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ):
        env.pop(name, None)
    env.update(
        PYTHONPATH=str(REPO / "python") + os.pathsep + env.get("PYTHONPATH", ""),
        PYTHONUNBUFFERED="1",
        P_IP=config.prefill_host,
        D_IP=config.decode_host,
        MODEL_PATH=config.model_path,
        LOG_DIR=str(root),
        GRAPH_MODE="graph",
        PREFILL_CAPACITY=str(config.prompt_capacity),
        DECODE_CAPACITY=str(config.decode_capacity),
        CONTEXT_LENGTH=str(config.context_length),
        MAX_TOTAL_TOKENS=str(config.max_total_tokens),
        P_PORT=str(config.prefill_port),
        D_PORT=str(config.decode_port),
        BOOTSTRAP_PORT=str(config.bootstrap_port),
        PAGE_SIZE=str(config.page_size),
        SKIP_SERVER_WARMUP="1",
        MEMPOOL_TIMEOUT=str(config.startup_timeout),
        SGLANG_NPU_MEMPOOL_SHARE="0",
        SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=",".join(map(str, config.numa_nodes)),
        HCCL_SOCKET_IFNAME=config.interface,
        GLOO_SOCKET_IFNAME=config.interface,
    )
    if target:
        env[CONFIG_ENV], env[RUN_ENV] = str(args.config), args.run_id
    return env


def request_json(
    url: str, *, payload: dict[str, Any] | None = None, timeout: float = 10
) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)  # type: ignore[no-any-return]


class Runner:
    def __init__(
        self, args: argparse.Namespace, config: RouterConfig, peer: Peer, root: Path
    ) -> None:
        self.args, self.config, self.peer, self.root = args, config, peer, root
        self.is_p = args.role == "prefill"
        self.host = config.prefill_host if self.is_p else config.decode_host
        self.report: dict[str, Any] = dict(
            status="running",
            role=args.role,
            mode=args.mode,
            coverage="ticket01 cold requests only",
            user_accepted=False,
            stages=[],
        )
        self.cases: list[dict[str, Any]] = []
        self.reference: list[dict[str, Any]] = []

    def preflight(self) -> None:
        if self.config.interface not in {name for _, name in socket.if_nameindex()}:
            raise ValueError("configured network interface does not exist")
        for node in self.config.numa_nodes:
            if not Path(f"/sys/devices/system/node/node{node}").is_dir():
                raise ValueError(f"configured NUMA node {node} does not exist")
        manifest = model_identity(Path(self.config.model_path), "glm52")
        diff = subprocess.check_output(
            ["git", "diff", "HEAD", "--", "python", "ascend-mempool-test"], cwd=REPO
        )
        (self.root / "code.patch").write_bytes(diff)
        untracked = subprocess.check_output(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                "python",
                "ascend-mempool-test",
            ],
            cwd=REPO,
            text=True,
        ).splitlines()
        identity = dict(
            run_id=self.args.run_id,
            mode=self.args.mode,
            config=msgspec.to_builtins(self.config),
            git_sha=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
            ).strip(),
            tracked_diff_sha256=hashlib.sha256(diff).hexdigest(),
            untracked_sha256={
                p: hashlib.sha256((REPO / p).read_bytes()).hexdigest()
                for p in untracked
            },
            model=manifest,
            versions={
                name: version(name)
                for name in ("torch", "torch-npu", "sgl-kernel-npu", "memfabric-hybrid")
            },
        )
        self.peer.send("identity", value=identity)
        if self.peer.expect("identity")["value"] != identity:
            raise ValueError(
                "P/D code, config, runtime packages or model identity differ"
            )
        write_json(self.root / "identity.json", identity)
        self.report["identity"] = identity
        meminfo = Path("/proc/meminfo").read_text()
        (self.root / "meminfo.txt").write_text(meminfo)
        match = re.search(r"^MemAvailable:\s+(\d+)", meminfo, re.M)
        if match is None:
            raise ValueError("/proc/meminfo does not report MemAvailable")
        available = int(match.group(1)) * 1024
        required = (176 if self.is_p else 16) * 2**30
        if available < required:
            raise ValueError(
                f"BM private replicas need at least {required} host bytes; available={available}"
            )
        with (self.root / "npu-smi.txt").open("w") as report:
            subprocess.run(
                ["npu-smi", "info"],
                stdout=report,
                stderr=subprocess.STDOUT,
                check=True,
            )
        ports = {
            self.config.prefill_port if self.is_p else self.config.decode_port,
            10000,
            *range(25670, 25702),
        }
        if self.is_p:
            ports.update(
                {
                    self.config.main_port,
                    self.config.control_port,
                    self.config.bootstrap_port,
                    24670,
                    *range(19000, 19016),
                }
            )
        for port in ports:
            available_port(self.host, port)
        self.peer.barrier("preflight")

    def components(self) -> None:
        root = self.root / "fetch"
        env = service_environment(self.config, self.args, root, target=False)
        command = [
            sys.executable,
            "-u",
            str(SCRIPTS / "verify_fetch.py"),
            "--rank",
            "0" if self.is_p else "1",
            "--head-ip",
            self.config.prefill_host,
            "--device-id",
            "0",
            "--nic-url",
            f"tcp://{self.host}:24770",
            "--store-port",
            "18873",
            "--control-port",
            "18874",
            "--pool-id",
            "0",
            "--layers",
            "78",
            "--s-p",
            "8",
            "--s-d",
            "16",
            "--heads",
            "1",
            "--kv-dim",
            "576",
            "--graph-rows",
            "16",
            "--active-rows",
            "3",
            "--topk",
            "2048",
            "--block-dims",
            "24",
            "48",
            "--replay-cycles",
            "2",
            "--warmup",
            "3",
            "--timeout",
            str(self.config.operation_timeout),
            "--report",
            str(root / "report.json"),
        ]
        self.peer.barrier("fetch-start")
        with Processes(root, env) as jobs:
            child = jobs.start("fetch", command)
            jobs.wait(
                "fetch",
                child,
                timeout=self.config.operation_timeout,
                check=self.peer.check,
            )
        if json.loads((root / "report.json").read_text()).get("status") != "passed":
            raise ValueError("NPU P-only/D-only/mixed copy adapter did not pass")
        if self.is_p:
            receive_log(self.peer, root / "decode-report.json")
        else:
            send_log(self.peer, root / "report.json")
        self.peer.barrier("fetch-done")
        self.report["stages"].append("fetch")

    def rejected_inputs(self, base: str) -> dict[str, Any]:
        before = request_json(base + "/status")
        cases: list[dict[str, Any]] = [
            dict(
                input_ids=[7] * (self.config.page_size + 1),
                sampling_params=dict(temperature=0, max_new_tokens=1),
            ),
            dict(
                input_ids=[7] * 8256,
                sampling_params=dict(temperature=0, max_new_tokens=1),
            ),
            dict(
                input_ids=[7] * self.config.page_size,
                sampling_params=dict(temperature=0, max_new_tokens=129),
            ),
            dict(text="unsupported"),
        ]
        for case in cases:
            try:
                request_json(base + "/generate", payload=case)
            except urllib.error.HTTPError as exc:
                if exc.code != 400:
                    raise
            else:
                raise ValueError("unsupported request was admitted")
        after = request_json(base + "/status")
        if any(
            before[k] != after[k]
            for k in ("active", "ledger", "directory", "completed")
        ):
            raise ValueError("rejected requests changed the resource ledger")
        return dict(status="passed", cases=len(cases), ledger_unchanged=True)

    def timeout_case(
        self, jobs: Processes, server: subprocess.Popen[bytes], root: Path, base: str
    ) -> None:
        """Pause only our P workers, reserve D, and observe a real Main timeout.

        Main stays alive in its own process group. D must retain its native and
        alive allocation; both runners then clean their own child groups. This
        intentionally waits operation_timeout after the successful cold cases.
        """
        self.peer.barrier("timeout-start")
        if self.is_p:
            if server not in jobs.children:
                raise RuntimeError("timeout injection may only pause an owned child")
            before = None
            deadline = time.monotonic() + self.config.operation_timeout + 15
            os.killpg(server.pid, signal.SIGSTOP)
            try:
                case = self.cases[0]
                try:
                    request_json(
                        base + "/generate",
                        payload={k: case[k] for k in ("input_ids", "sampling_params")},
                        timeout=2,
                    )
                except TimeoutError:
                    pass  # Deliberate client disconnect is not cleanup evidence.
                else:
                    raise ValueError("paused P unexpectedly completed a request")
                while True:
                    self.peer.check()
                    after = request_json(base + "/status", timeout=10)
                    if (
                        before is None
                        and after["active"]
                        and "decode" in after["active"]["allocations"]
                    ):
                        before = after
                    if after["fault"]:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "Main did not fail-stop after P heartbeat loss"
                        )
                    time.sleep(0.5)
                if before is None:
                    raise ValueError(
                        "timeout happened before D actually reserved native storage"
                    )
                try:
                    request_json(
                        base + "/generate",
                        payload={k: case[k] for k in ("input_ids", "sampling_params")},
                    )
                except urllib.error.HTTPError as exc:
                    if exc.code != 503:
                        raise
                else:
                    raise ValueError("fail-stop admitted another request")
                write_json(
                    root / "timeout-snapshots.json", dict(before=before, after=after)
                )
                self.peer.send("fault-result", rid=after["active"]["rid"])
                receive_log(self.peer, root / "decode-fault.log")
                evidence = verify_mempool_min.check_timeout(
                    before,
                    after,
                    (root / "decode-fault.log").read_text(errors="replace"),
                )
                self.peer.send("fault-verified", evidence=evidence)
            finally:
                # SIGTERM alone cannot reap a stopped process group.
                try:
                    os.killpg(server.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        else:
            # Native workers are expected to exit on Main's error broadcast.
            # Keep the paired runner alive to collect logs and perform cleanup.
            self.peer.expect("fault-result")
            send_log(self.peer, root / "decode.log")
            evidence = self.peer.expect("fault-verified")["evidence"]
        write_json(root / "timeout.json", evidence)
        self.report["timeout_fail_stop"] = evidence

    def final_timeout_evidence(self, root: Path) -> None:
        """Check the complete D log after both owned process groups have exited."""
        if self.is_p:
            receive_log(self.peer, root / "decode-fault-final.log")
            snapshots = json.loads((root / "timeout-snapshots.json").read_text())
            evidence = verify_mempool_min.check_timeout(
                snapshots["before"],
                snapshots["after"],
                (root / "decode-fault-final.log").read_text(errors="replace"),
            )
            evidence["process_cleanup_verified"] = True
            self.peer.send("fault-final-verified", evidence=evidence)
        else:
            send_log(self.peer, root / "decode.log")
            evidence = self.peer.expect("fault-final-verified")["evidence"]
        write_json(root / "timeout.json", evidence)
        self.report["timeout_fail_stop"] = evidence
        self.report["paired_process_cleanup"] = "passed; both owned groups stopped"

    def stage(self, *, target: bool) -> None:
        name = "target" if target else "reference"
        root = self.root / name
        env = service_environment(self.config, self.args, root, target=target)
        self.peer.barrier(name + "-start")
        with Processes(root, env) as jobs:
            router = None
            if self.is_p and target:
                router = jobs.start(
                    "router",
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "sglang.srt.disaggregation.ascend.mempool_router",
                        "--config",
                        str(self.args.config),
                        "--run-id",
                        self.args.run_id,
                    ],
                )
            server = jobs.start(
                "launcher",
                ["bash", str(SCRIPTS / "run_service.sh"), self.args.role, "formal"],
            )

            def watch() -> None:
                jobs.alive("NPU service", server)
                if router is not None:
                    jobs.alive("Router", router)
                self.peer.check()

            wait_http(
                self.config.backend(self.args.role) + "/ready",
                self.config.startup_timeout,
                watch,
            )
            self.peer.barrier(name + "-backends-up")
            base = f"http://{self.config.prefill_host}:{self.config.main_port}"
            if self.is_p:
                if not target:
                    router = jobs.start(
                        "reference-router",
                        [
                            sys.executable,
                            "-m",
                            "sglang_router.launch_router",
                            "--pd-disaggregation",
                            "--policy",
                            "round_robin",
                            "--prefill",
                            self.config.backend("prefill"),
                            str(self.config.bootstrap_port),
                            "--decode",
                            self.config.backend("decode"),
                            "--host",
                            self.config.prefill_host,
                            "--port",
                            str(self.config.main_port),
                            "--mini-lb",
                            "--request-timeout-secs",
                            str(int(self.config.operation_timeout)),
                        ],
                    )
                wait_http(
                    base + ("/ready" if target else "/health"),
                    self.config.startup_timeout,
                    watch,
                )
            self.peer.barrier(name + "-ready")
            if self.args.mode == "serve":
                print(
                    f"SERVING {self.args.role}; unified entry: {base}/generate",
                    flush=True,
                )
                while True:
                    watch()
                    if self.is_p:
                        status = request_json(base + "/status")
                        if status["fault"]:
                            raise RuntimeError(status["fault"])
                    time.sleep(1)
            if self.is_p:
                snapshots, results = [], []
                if target:
                    write_json(
                        root / "input-rejections.json", self.rejected_inputs(base)
                    )
                for index, case in enumerate(self.cases):
                    watch()
                    result = request_json(
                        base + "/generate",
                        payload={k: case[k] for k in ("input_ids", "sampling_params")},
                        timeout=self.config.operation_timeout,
                    )
                    # Preserve failing outputs too, before any acceptance assertion.
                    results.append(result)
                    if target:
                        snapshots.append(request_json(base + "/status", timeout=30))
                    write_json(
                        root / "responses.json", dict(cases=self.cases, results=results)
                    )
                    write_json(root / "snapshots.json", snapshots)
                    verify_mempool_min.check_response(case, result)
                    if (
                        target
                        and result["output_ids"] != self.reference[index]["output_ids"]
                    ):
                        raise ValueError(
                            f"{case['name']}: full output IDs differ from formal reference"
                        )
                deadline = time.monotonic() + 60
                while True:
                    watch()
                    self.peer.send("stage-control", action="snapshot")
                    receive_log(self.peer, root / "decode.log")
                    try:
                        evidence = (
                            verify_mempool_min.check_target(
                                root / "prefill.log",
                                root / "decode.log",
                                snapshots,
                                self.cases,
                                page_size=self.config.page_size,
                                prompt_capacity=self.config.prompt_capacity,
                                decode_capacity=self.config.decode_capacity,
                            )
                            if target
                            else verify_service.check_logs(
                                [root / "prefill.log"],
                                [root / "decode.log"],
                                len(self.cases),
                                layers=78,
                                indexer_layer_ids=verify_mempool_min.PRODUCERS,
                                graph=True,
                            )
                        )
                        break
                    except (ValueError, RuntimeError, KeyError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(1)
                write_json(root / "evidence.json", evidence)
                if not target:
                    self.reference = results
                self.peer.send("stage-control", action="done")
            else:
                while True:
                    message = self.peer.expect("stage-control", check=watch)
                    if message == dict(action="done"):
                        break
                    if message != dict(action="snapshot"):
                        raise ValueError("unknown runner action")
                    send_log(self.peer, root / "decode.log")
            if target:
                # Freeze the successful normal logs before the intentional fault.
                for role in ("prefill", "decode") if self.is_p else ("decode",):
                    (root / f"{role}-normal.log").write_bytes(
                        (root / f"{role}.log").read_bytes()
                    )
                self.timeout_case(jobs, server, root, base)
        self.peer.barrier(name + "-stopped")
        if target:
            self.final_timeout_evidence(root)
        self.report["stages"].append(name)
        write_json(self.root / "summary.json", self.report)

    def run(self) -> None:
        self.preflight()
        if self.args.mode == "acceptance":
            self.components()
            if self.is_p:
                self.cases = verify_mempool_min.make_cases(
                    self.config.model_path, self.config.page_size
                )
                write_json(self.root / "cases.json", self.cases)
            self.peer.barrier("fixtures-ready")
            self.stage(target=False)
        self.stage(target=True)
        self.report.update(
            status="passed",
            accuracy="full output IDs equal to same-length formal reference",
            hardware_execution="completed by user runner",
            prefix="not covered",
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = RouterConfig.read(str(args.config))
    root = args.log_root / args.run_id / args.role
    if args.describe:
        print(
            json.dumps(
                dict(
                    role=args.role,
                    mode=args.mode,
                    config=msgspec.to_builtins(config),
                    logs=str(root),
                    stages=[
                        "paired preflight",
                        "P-only/D-only/mixed NPU fetch",
                        "formal reference 4096+128",
                        "Main/SR target and cleanup",
                        "timeout/fail-stop and paired cleanup",
                    ]
                    if args.mode == "acceptance"
                    else ["paired preflight", "Main and P/D persistent services"],
                    service_command=[
                        "bash",
                        str(SCRIPTS / "run_service.sh"),
                        args.role,
                        "formal",
                    ],
                    target_environment={
                        k: v
                        for k, v in service_environment(
                            config, args, root, target=True
                        ).items()
                        if k
                        in (
                            CONFIG_ENV,
                            RUN_ENV,
                            "CONTEXT_LENGTH",
                            "PREFILL_CAPACITY",
                            "DECODE_CAPACITY",
                            "PAGE_SIZE",
                            "MAX_TOTAL_TOKENS",
                            "SKIP_SERVER_WARMUP",
                        )
                    },
                ),
                indent=2,
            )
        )
        return 0
    os.chdir(REPO)
    root.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = dict(status="failed", mode=args.mode, role=args.role)

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt(f"runner received signal {signum}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    peer_args = argparse.Namespace(
        role=args.role,
        prefill_host=config.prefill_host,
        decode_host=config.decode_host,
        control_port=config.runner_port,
        timeout=config.startup_timeout,
    )
    try:
        with Peer(connect(peer_args), config.startup_timeout) as peer:
            runner = Runner(args, config, peer, root)
            report = runner.report
            try:
                runner.run()
            except BaseException as exc:
                report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                try:
                    peer.send("error", message=str(exc))
                except OSError:
                    pass
                raise
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        print(report["error"], file=sys.stderr)
        return 1
    finally:
        write_json(root / "summary.json", report)
        if args.role == "prefill":
            write_json(root.parent / "summary.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
