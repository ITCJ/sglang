"""K1-02 functional evidence with two model starts and explicit reduced coverage."""

from __future__ import annotations

import copy
import errno
import json
import socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from sglang.srt.disaggregation.ascend.mempool_router import MainRouter
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import RouterConfig
else:
    from ascend_mempool_pd.mempool_router import MainRouter
    from ascend_mempool_pd.mempool_router_protocol import RouterConfig

SERVICE_STAGES = ("target", "restart")


def _tcp_port_state(host: str, port: int) -> list[dict[str, str]]:
    """Snapshot matching Linux sockets, including wildcard and mapped IPv4 binds."""
    rows = []
    host = socket.gethostbyname(host)
    for name, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
        path = Path("/proc/net") / name
        try:
            lines = path.read_text().splitlines()[1:]
        except FileNotFoundError:
            if name == "tcp6":
                continue
            raise
        for line in lines:
            fields = line.split()
            address, number = fields[1].split(":")
            if int(number, 16) != port:
                continue
            raw = b"".join(
                int(address[i : i + 8], 16).to_bytes(4, sys.byteorder)
                for i in range(0, len(address), 8)
            )
            local = socket.inet_ntop(family, raw)
            if local not in (host, "0.0.0.0", "::", "::ffff:" + host):
                continue
            rows.append(
                dict(
                    local=fields[1],
                    remote=fields[2],
                    inode=fields[9],
                    state={"06": "TIME_WAIT", "0A": "LISTEN", "01": "ESTABLISHED"}.get(
                        fields[3], fields[3]
                    ),
                )
            )
    return rows


def released_port(
    host: str,
    port: int,
    *,
    timeout: float = 0,
    evidence_path: Path | None = None,
    check: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Require successful bind/listen; wait only for observed Linux TIME_WAIT."""
    started = time.monotonic()
    deadline = started + max(0, timeout)
    evidence: dict[str, Any] = dict(host=host, port=port, status="checking")

    def save() -> None:
        evidence["waited_seconds"] = time.monotonic() - started
        if evidence_path is not None:
            evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")

    try:
        while True:
            check()
            try:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind((host, port))
                    probe.listen(1)
                break
            except OSError as exc:
                error = OSError(
                    exc.errno, f"service port {host}:{port} not released: {exc}"
                )
                if exc.errno != errno.EADDRINUSE:
                    raise error from exc
                try:
                    states = _tcp_port_state(host, port)
                except OSError as snapshot_error:
                    evidence["snapshot_error"] = str(snapshot_error)
                    raise error from exc
                evidence.setdefault("first_blocked", states)
                evidence["last_blocked"] = states
                save()
                if not states or any(row["state"] != "TIME_WAIT" for row in states):
                    raise error from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"service port {host}:{port} still in TIME_WAIT after timeout"
                    ) from exc
                time.sleep(min(1, remaining))
        evidence["status"] = "passed"
        return evidence
    except BaseException as exc:
        evidence.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        save()


def capacity_check(
    config: RouterConfig, run_id: str, registrations: dict[str, Any]
) -> dict[str, Any]:
    """Replay real registrations through admission; do not start another model."""
    router = MainRouter(config, run_id)
    router.register(registrations["prefill"])
    before = router.status()["ledger"]
    decode = copy.deepcopy(registrations["decode"])
    decode["ranks"][-1]["hbm_pages"] = (
        config.prompt_capacity + config.decode_capacity
    ) // config.page_size - 1
    try:
        router.register(decode)
    except ValueError as exc:
        if "native capacities" not in str(exc):
            raise
    else:
        raise ValueError("capacity replay accepted an insufficient rank")
    if router.status()["ledger"] != before:
        raise ValueError("capacity rejection changed quotas")
    return dict(
        status="passed",
        ledger_unchanged=True,
        execution="CPU replay of actual NPU registration",
    )


def check_registration(
    registrations: dict[str, Any], *, config: RouterConfig | None = None
) -> None:
    if config is not None and (
        config.prefill_shared or config.decode_shared or config.prefill_index_shared
    ):
        from verify_mempool_shared import check_registration as check_shared

        check_shared(registrations, config)
        return
    for role in ("prefill", "decode"):
        rows = registrations[role]["ranks"]
        if [r["rank"] for r in rows] != list(range(16)):
            raise ValueError("session registration lacks all ranks")
        for rank in rows:
            if rank["replicas"] != dict(
                alive_main=16, alive_index=16, prefix_main=16, prefix_index=16
            ):
                raise ValueError("share=0 replica counts changed")
            regions = rank["replica_locations"]
            expected = (
                {"alive_main", "alive_index", "prefix_main", "prefix_index"}
                if role == "prefill"
                else {"alive_main"}
            )
            if set(regions) != expected:
                raise ValueError("session registration lacks replica regions")
            for value in regions.values():
                if (
                    value["owner"] != rank["rank"]
                    or value["members"] != [rank["rank"]]
                    or value["numa_node"] != rank["numa_node"]
                    or value["world_size"] != 2
                    or value["participant"] != (0 if role == "prefill" else 1)
                    or not value["session"]
                ):
                    raise ValueError("share=0 session owner/member mismatch")


def log_records(path: Path, kind: str) -> list[dict[str, Any]]:
    marker = f"mempool {kind} data="
    return [
        json.loads(line.split(marker, 1)[1])
        for line in path.read_text(errors="replace").splitlines()
        if marker in line
    ]


def accounting(root: Path, *, shared: bool = False) -> dict[str, Any]:
    snapshots = json.loads((root / "snapshots.json").read_text())
    requests = {s["completed"][-1]["rid"] for s in snapshots}
    prefix = root / "prefill-normal.log"
    if not prefix.exists():
        prefix = root / "prefill.log"
    records = log_records(prefix, "ready_accounting")
    if not requests or {r["rid"] for r in records} != requests:
        raise ValueError("missing ready accounting for completed requests")
    if not shared and any(r["ready_tokens"] != r["counter_tokens"] for r in records):
        raise ValueError("bitmap and old counter differ on the same event")
    cases = json.loads((root / "responses.json").read_text())["cases"]
    for snapshot, case in zip(snapshots, cases):
        rid = snapshot["completed"][-1]["rid"]
        if max(r["ready_tokens"] for r in records if r["rid"] == rid) != len(
            case["input_ids"]
        ):
            raise ValueError("ready accounting never reached the complete prompt")
    for role in ("prefill", "decode"):
        path = root / f"{role}-normal.log"
        if not path.exists():
            path = root / f"{role}.log"
        released = log_records(path, "free_accounting")
        for rid in requests:
            rows = [r for r in released if r["rid"] == rid]
            if {r["rank"] for r in rows} != set(range(16)) or any(
                r["readers_remaining"] != 0 for r in rows
            ):
                raise ValueError(
                    "free_ack lacks zero-reader evidence for every replica"
                )
    return dict(
        status="passed",
        requests=len(requests),
        equivalent_events=None if shared else len(records),
        ready_events=len(records),
        reader_counts_zero=True,
    )


def summarize(
    root: Path, *, config: RouterConfig | None = None, mode: str | None = None
) -> dict[str, Any]:
    import verify_mempool_shared

    rounds = []
    index_checks = (
        mode == "index-sharing-acceptance"
        if mode is not None
        else bool(config and config.prefill_index_shared and not config.prefix_shared)
    )
    expected_requests = 9 if index_checks else 6
    for name in SERVICE_STAGES:
        stage = root / name

        def read(path: str) -> Any:
            return json.loads((stage / path).read_text())

        evidence, answers = read("evidence.json"), read("answers.json")
        if (
            evidence["status"] != "passed"
            or evidence["requests"] != expected_requests
            or evidence["prefix"]["status"] != "passed"
        ):
            raise ValueError(f"{name}: incomplete model/content/lifecycle evidence")
        if len(answers) != expected_requests or any(
            a["status"] != "passed" for a in answers
        ):
            raise ValueError(f"{name}: logical answers failed")
        rejections = read("input-rejections.json")
        if rejections["status"] != "passed" or not rejections["ledger_unchanged"]:
            raise ValueError(f"{name}: input rejection failed")
        shared = (
            verify_mempool_shared.check_stage(stage, config)
            if config is not None
            else None
        )
        if config is None:
            check_registration(read("registrations.json"))
        for filename in (
            "process-cleanup.json",
            "ports-released.json",
            "capacity-replay.json",
        ):
            if read(filename)["status"] != "passed":
                raise ValueError(f"{name}: {filename} failed")
        rounds.append(
            dict(
                stage=name,
                status="passed",
                accounting=accounting(
                    stage, shared=bool(config and config.prefill_shared)
                ),
                sharing=shared,
            )
        )
        if index_checks:
            from mempool_logical_cases import check_index_accuracy

            responses = read("responses.json")
            accuracy = check_index_accuracy(
                responses["cases"], responses["results"], read("snapshots.json")
            )
            if accuracy["status"] != "passed" or accuracy != read(
                "index-accuracy.json"
            ):
                raise ValueError(
                    f"{name}: Index K logical accuracy failed or report differs"
                )
            rounds[-1]["index_accuracy"] = accuracy
    timeout = json.loads((root / "target/timeout.json").read_text())
    if (
        timeout["status"] != "passed"
        or not timeout.get("process_cleanup_verified")
        or timeout.get("worker_exit") in (None, 0)
    ):
        raise ValueError("session fail-stop was not verified")
    fetch = json.loads((root / "fetch/decode-report.json").read_text())
    if fetch["status"] != "passed":
        raise ValueError("P/D/mixed fetch smoke failed")
    return dict(
        status="passed",
        coverage="K1-08 reduced functional acceptance"
        if config is not None and config.prefix_shared
        else "K1-07 reduced functional acceptance"
        if config is not None and config.prefill_index_shared
        else "shared decode functional regression (without ordering probes)"
        if config is not None and config.decode_shared
        else "K1-05 reduced functional acceptance"
        if config is not None
        else "K1-02 reduced functional acceptance",
        service_rounds=len(SERVICE_STAGES),
        shutdown="SGLang default parent/process cleanup; no per-rank graceful BM release requirement",
        rounds=rounds,
        user_accepted=False,
        limitations=[
            "Prefix verification uses computed-source snapshots and restored native HBM; alive/prefix/fill DRAM contents are not read back.",
            "V3 uses independent fixture answers; no fresh formal-service comparison or cold A/C stability run.",
            "V8 capacity rejection replays actual registration on CPU; no isolated worker failure run.",
            "Physical NUMA placement and performance are not accepted by this run.",
            "Service teardown uses default process cleanup and restart checks; explicit BM leave/uninitialize and post-exit physical DRAM page counts are not accepted by this run.",
        ],
        retained="ABCE, load/EOS, KV content, Graph, source fetch, slot reuse, ready/free accounting, fail-stop, restart and paired cleanup",
    )
