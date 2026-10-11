"""Run paired ticket10 acceptance inside two existing NPU containers.

Start once per container with matching run ID and hosts. P drives requests;
D follows the same fixed stages. No SSH or remote command execution is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
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
from typing import Any, Callable, Optional, Sequence

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
import verify_service
from ticket10_processes import Peer, Processes

PRODUCERS = [
    0,
    1,
    2,
    6,
    10,
    14,
    18,
    22,
    26,
    30,
    34,
    38,
    42,
    46,
    50,
    54,
    58,
    62,
    66,
    70,
    74,
]
MODES = ("native-eager", "native-graph", "formal-eager", "formal-graph")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--prefill-host", required=True)
    parser.add_argument("--decode-host", required=True)
    parser.add_argument(
        "--run-id", required=True, help="Unique ID, identical on both sides"
    )
    parser.add_argument(
        "--model-path", type=Path, default=Path("/data_lib/data/models/GLM-5.2-w4a8")
    )
    parser.add_argument(
        "--glm51-model-path", type=Path, help="Also run the GLM-5.1 regression matrix"
    )
    parser.add_argument(
        "--suite", choices=("all", "components", "models", "glm51-quick"), default="all"
    )
    parser.add_argument(
        "--log-root", type=Path, default=Path("/home/cryang/log/ticket10")
    )
    parser.add_argument("--control-port", type=int, default=18890)
    parser.add_argument(
        "--timeout", type=float, default=7200, help="Maximum seconds per stage"
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Show plan without opening sockets or launching processes",
    )
    args = parser.parse_args(argv)
    if args.suite == "glm51-quick" and args.glm51_model_path is None:
        args.glm51_model_path = Path("/data_lib/data/models/GLM-5.1-w4a8")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.run_id):
        parser.error("run-id must be a simple directory name, at most 80 characters")
    if (
        not 1 <= args.control_port <= 65535
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        parser.error("invalid control port or timeout")
    if args.prefill_host == args.decode_host:
        parser.error("this runner requires separate P and D hosts, each with TP16")
    return args


def connect(args: argparse.Namespace) -> socket.socket:
    print(f"Waiting for peer at {args.prefill_host}:{args.control_port}", flush=True)
    deadline = time.monotonic() + args.timeout
    if args.role == "prefill":
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((args.prefill_host, args.control_port))
            listener.listen(1)
            listener.settimeout(args.timeout)
            connection, _ = listener.accept()
    else:
        while True:
            try:
                connection = socket.create_connection(
                    (args.prefill_host, args.control_port), timeout=3
                )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("P runner was not reachable") from None
                time.sleep(1)
    connection.settimeout(args.timeout)
    return connection


def model_identity(path: Path, label: str) -> dict[str, Any]:
    config = json.loads((path / "config.json").read_text())
    expected = PRODUCERS if label == "glm52" else list(range(78))
    kinds = config.get("indexer_types")
    if kinds is None:
        if label != "glm51" or config.get("index_topk_freq", 1) != 1:
            raise ValueError(
                f"{label}: expected explicit shared layers or all-full GLM-5.1"
            )
        actual = list(range(config["num_hidden_layers"]))
    else:
        if len(kinds) != 78 or any(kind not in ("full", "shared") for kind in kinds):
            raise ValueError(f"{label}: invalid indexer_types")
        actual = [i for i, kind in enumerate(kinds) if kind == "full"]
    if (
        config["num_hidden_layers"] != 78
        or actual != expected
        or config.get("index_topk") != 2048
        or config.get("kv_lora_rank") != 512
        or config.get("qk_rope_head_dim") != 64
        or config.get("index_head_dim") != 128
    ):
        raise ValueError(
            f"{label}: model differs from this ticket's 78-layer/2048-topk contract"
        )
    # Hash deployment metadata/tokenizer, not multi-GB weight shards.
    files = {path / "config.json", path / "quant_model_description.json"}
    for pattern in (
        "tokenizer*",
        "chat_template*",
        "special_tokens_map.json",
        "added_tokens.json",
        "generation_config.json",
        "vocab.*",
        "merges.txt",
    ):
        files.update(p for p in path.glob(pattern) if p.is_file())
    return dict(
        indexer_ids=expected,
        manifest={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)
        },
    )


def available_port(host: str, port: int) -> None:
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"{host}:{port} is occupied; stop the previous test service first"
            ) from exc


def wait_http(url: str, timeout: float, check: Callable[[], None]) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        check()
        try:
            with opener.open(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"service not ready: {url}")
        time.sleep(1)


def send_log(peer: Peer, path: Path) -> None:
    # Snapshot a growing log at a fixed byte length, with bounded frames.
    size = path.stat().st_size if path.exists() else 0
    if size > 128 * 1024 * 1024:
        raise ValueError("decode log exceeds 128 MiB; inspect it manually")
    peer.send("log", size=size)
    if size:
        with path.open("rb") as stream:
            remaining = size
            while remaining:
                data = stream.read(min(16384, remaining))
                if not data:
                    raise RuntimeError("decode log shrank during snapshot")
                peer.send("chunk", data=base64.b64encode(data).decode())
                remaining -= len(data)


def receive_log(peer: Peer, path: Path) -> None:
    remaining = peer.expect("log")["size"]
    if type(remaining) is not int or not 0 <= remaining <= 128 * 1024 * 1024:
        raise ValueError("invalid peer log size")
    with path.open("wb") as stream:
        while remaining:
            data = base64.b64decode(peer.expect("chunk")["data"], validate=True)
            if not data or len(data) > min(16384, remaining):
                raise ValueError("invalid peer log chunk")
            stream.write(data)
            remaining -= len(data)


class Acceptance:
    def __init__(self, args: argparse.Namespace, peer: Peer, root: Path) -> None:
        self.args, self.peer, self.root = args, peer, root
        self.is_p = args.role == "prefill"
        self.host = args.prefill_host if self.is_p else args.decode_host
        self.env = dict(os.environ)
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.env.pop(key, None)
        self.env.update(
            PYTHONPATH=str(REPO / "python")
            + os.pathsep
            + self.env.get("PYTHONPATH", ""),
            PYTHONUNBUFFERED="1",
        )
        self.report: dict[str, Any] = dict(status="running", role=args.role, stages=[])

    def command(self, script: str, *args: str) -> list[str]:
        return [sys.executable, "-u", str(SCRIPTS / script), *args]

    def completed(self, stage: str) -> None:
        self.report["stages"].append(stage)
        write_json(self.root / "summary.json", self.report)
        print(f"PASS {stage}", flush=True)

    def preflight(self) -> None:
        identity = dict(
            run_id=self.args.run_id,
            suite=self.args.suite,
            prefill_host=self.args.prefill_host,
            decode_host=self.args.decode_host,
            git_sha=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            versions={
                name: version(name)
                for name in ("torch", "torch-npu", "sgl-kernel-npu", "memfabric-hybrid")
            },
        )
        # A shared SHA is insufficient when one checkout has local code edits.
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--", "python", "ascend-mempool-test"],
            text=True,
        )
        if dirty:
            raise RuntimeError(
                "commit or restore local code/test edits before paired acceptance"
            )
        if self.args.suite != "components":
            if self.args.suite != "glm51-quick":
                identity["glm52"] = model_identity(self.args.model_path, "glm52")
            if self.args.glm51_model_path:
                identity["glm51"] = model_identity(self.args.glm51_model_path, "glm51")
        self.peer.send("identity", value=identity)
        if self.peer.expect("identity")["value"] != identity:
            raise RuntimeError(
                "P/D run ID, suite, code, packages or model/tokenizer metadata differ"
            )
        write_json(self.root / "identity.json", identity)
        self.report["identity"] = identity
        available_port(self.host, 8000 if self.is_p else 8001)
        if self.is_p:
            available_port("127.0.0.1", 6699)
        self.peer.barrier("preflight")

    def components(self) -> None:
        rank = "0" if self.is_p else "1"
        root = self.root / "components"
        common = [
            "--rank",
            rank,
            "--head-ip",
            self.args.prefill_host,
            "--device-id",
            "0",
        ]
        commands = {
            "transfer": self.command(
                "verify_pd_transfer.py",
                *common,
                "--local-ip",
                self.host,
                "--layers",
                "78",
                "--indexer-layer-ids",
                *map(str, PRODUCERS),
                "--report",
                str(root / "transfer.json"),
            ),
            "fetch": self.command(
                "verify_fetch.py",
                *common,
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
                "--report",
                str(root / "fetch.json"),
            ),
        }
        for name, command in commands.items():
            self.peer.barrier(name)
            with Processes(root, self.env) as jobs:
                child = jobs.start(
                    name, command + ["--timeout", str(min(self.args.timeout, 600))]
                )
                jobs.wait(name, child, timeout=self.args.timeout, check=self.peer.check)
            result = json.loads((root / f"{name}.json").read_text())
            if (
                result.get("success") is not True
                if name == "transfer"
                else result.get("status") != "passed"
            ):
                raise RuntimeError(f"{name} report did not pass")
            self.peer.barrier(name + "-passed")
            self.completed(name)

    def check_lifecycle(
        self,
        root: Path,
        label: str,
        execution: str,
        watch: Callable[[], None],
        *,
        requests: int = 10,
    ) -> None:
        deadline = time.monotonic() + min(self.args.timeout, 180)
        while True:
            watch()
            self.peer.send("snapshot")
            receive_log(self.peer, root / "decode.log")
            try:
                result = verify_service.check_logs(
                    [root / "prefill.log"],
                    [root / "decode.log"],
                    requests,
                    layers=78,
                    indexer_layer_ids=PRODUCERS
                    if label == "glm52"
                    else list(range(78)),
                    graph=execution == "graph",
                )
                write_json(root / "service.json", result)
                return
            except (RuntimeError, ValueError, KeyError) as exc:
                if time.monotonic() >= deadline:
                    write_json(
                        root / "service.json", dict(status="failed", error=str(exc))
                    )
                    raise
                time.sleep(2)

    def model(self, label: str, model: Path, *, quick: bool = False) -> None:
        if quick and label != "glm51":
            raise ValueError("quick smoke is only defined for GLM-5.1")
        model_root = self.root / label
        cases = model_root / "cases.json"
        if self.is_p:
            with Processes(model_root, self.env) as jobs:
                child = jobs.start(
                    "prepare",
                    self.command(
                        "verify_model_tokens.py",
                        "prepare",
                        "--model-path",
                        str(model),
                        "--output",
                        str(cases),
                        *(["--quick"] if quick else []),
                    ),
                )
                jobs.wait(
                    "prepare", child, timeout=self.args.timeout, check=self.peer.check
                )
        self.peer.barrier(label + "-prepared")
        for mode_name in ("formal-graph",) if quick else MODES:
            mode, execution = mode_name.split("-")
            root = model_root / mode_name
            self.peer.barrier(label + "-" + mode_name)
            print(f"STAGE {label}/{mode_name}", flush=True)
            with Processes(root, self.env) as jobs:
                server = jobs.start(
                    "launcher",
                    ["bash", str(SCRIPTS / "run_service.sh"), self.args.role, mode],
                    P_IP=self.args.prefill_host,
                    D_IP=self.args.decode_host,
                    MODEL_PATH=str(model),
                    LOG_DIR=str(root),
                    GRAPH_MODE=execution,
                    CONTEXT_LENGTH="1024" if quick else "8192",
                    PREFILL_CAPACITY="512" if quick else "4096",
                    DECODE_CAPACITY="512" if quick else "4096",
                    MAX_TOTAL_TOKENS="8192",
                )

                def watch() -> None:
                    jobs.alive("model server", server)
                    self.peer.check()

                wait_http(
                    f"http://{self.host}:{8000 if self.is_p else 8001}/ready",
                    self.args.timeout,
                    watch,
                )
                self.peer.barrier("servers-ready")
                if self.is_p:
                    router = jobs.start(
                        "router",
                        [
                            sys.executable,
                            "-m",
                            "sglang_router.launch_router",
                            "--pd-disaggregation",
                            "--policy",
                            "round_robin",
                            "--prefill",
                            f"http://{self.args.prefill_host}:8000",
                            "8995",
                            "--decode",
                            f"http://{self.args.decode_host}:8001",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            "6699",
                            "--mini-lb",
                            "--request-timeout-secs",
                            "1800",
                        ],
                    )

                    def watch_router() -> None:
                        watch()
                        jobs.alive("router", router)

                    wait_http(
                        "http://127.0.0.1:6699/health",
                        self.args.timeout,
                        watch_router,
                    )
                    tokens = jobs.start(
                        "requests",
                        self.command(
                            "verify_model_tokens.py",
                            "run",
                            "--url",
                            "http://127.0.0.1:6699",
                            "--cases",
                            str(cases),
                            "--report",
                            str(root / "tokens.json"),
                        ),
                    )
                    jobs.wait(
                        "requests",
                        tokens,
                        timeout=self.args.timeout,
                        check=watch_router,
                    )
                    if mode == "formal":
                        self.check_lifecycle(
                            root,
                            label,
                            execution,
                            watch_router,
                            requests=8 if quick else 10,
                        )
                    self.peer.send("requests-done")
                else:
                    while True:
                        command = self.next_action(watch)
                        if command == "requests-done":
                            break
                        if command != "snapshot" or mode != "formal":
                            raise RuntimeError(f"unexpected request phase: {command}")
                        send_log(self.peer, root / "decode.log")
                self.peer.barrier("requests-checked")
            self.peer.barrier("servers-stopped")
            self.completed(label + "/" + mode_name)
        if quick:
            self.completed(label + "/quick-smoke")
            return
        if self.is_p:
            with Processes(model_root, self.env) as jobs:
                for candidate in MODES[1:]:
                    child = jobs.start(
                        "compare-" + candidate,
                        self.command(
                            "verify_model_tokens.py",
                            "compare",
                            str(model_root / "native-eager/tokens.json"),
                            str(model_root / candidate / "tokens.json"),
                        ),
                    )
                    jobs.wait(
                        "token comparison", child, timeout=60, check=self.peer.check
                    )
        self.peer.barrier(label + "-compared")
        self.completed(label + "/token-comparison")

    def next_action(self, watch: Callable[[], None]) -> str:
        # D accepts only snapshot/done in this phase; no remotely supplied command.
        deadline = time.monotonic() + self.args.timeout
        while not self.peer.pending:
            watch()
            if time.monotonic() >= deadline:
                raise TimeoutError("waiting for P requests")
            time.sleep(0.1)
        return str(self.peer.pending.popleft()["kind"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = args.log_root / args.run_id / args.role
    if args.describe:
        print(
            json.dumps(
                dict(
                    role=args.role,
                    logs=str(root),
                    suite=args.suite,
                    components=["transfer", "fetch"]
                    if args.suite in ("all", "components")
                    else [],
                    model_modes=("formal-graph",)
                    if args.suite == "glm51-quick"
                    else MODES
                    if args.suite != "components"
                    else [],
                    models=["glm51"]
                    if args.suite == "glm51-quick"
                    else (
                        []
                        if args.suite == "components"
                        else ["glm52"] + (["glm51"] if args.glm51_model_path else [])
                    ),
                    glm51_regression=args.suite != "components"
                    and args.glm51_model_path is not None,
                ),
                indent=2,
            )
        )
        return 0
    os.chdir(REPO)
    root.mkdir(parents=True, exist_ok=False)

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt(f"received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    runner = None
    try:
        with Peer(connect(args), args.timeout) as peer:
            runner = Acceptance(args, peer, root)
            try:
                runner.preflight()
                if args.suite in ("all", "components"):
                    runner.components()
                if args.suite == "glm51-quick":
                    runner.model("glm51", args.glm51_model_path, quick=True)
                elif args.suite != "components":
                    runner.model("glm52", args.model_path)
                    if args.glm51_model_path:
                        runner.model("glm51", args.glm51_model_path)
                peer.barrier("finished")
                runner.report.update(
                    status="automated_checks_passed",
                    acceptance="pending user review of output, deployment and performance",
                    glm51_regression="passed"
                    if "glm51/token-comparison" in runner.report["stages"]
                    else "quick_smoke_passed"
                    if "glm51/quick-smoke" in runner.report["stages"]
                    else "not run",
                )
                write_json(root / "summary.json", runner.report)
            except BaseException as exc:
                try:
                    peer.send("error", message=f"{type(exc).__name__}: {exc}")
                except OSError:
                    pass
                raise
    except BaseException as exc:
        report = runner.report if runner else {}
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        write_json(root / "summary.json", report)
        print(f"FAILED: {exc}; logs: {root}", file=sys.stderr, flush=True)
        return 1
    print(f"AUTOMATED_CHECKS_PASSED: {root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
