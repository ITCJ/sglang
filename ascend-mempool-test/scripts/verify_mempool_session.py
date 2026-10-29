"""K1-02 functional evidence with two model starts and explicit reduced coverage."""

from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.disaggregation.ascend.mempool_router import MainRouter
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import RouterConfig
else:
    from ascend_mempool_pd.mempool_router import MainRouter
    from ascend_mempool_pd.mempool_router_protocol import RouterConfig

SERVICE_STAGES = ("target", "restart")


def released_port(host: str, port: int) -> None:
    """Permit closed connections in TIME_WAIT while detecting live listeners."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        probe.listen(1)


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


def check_registration(registrations: dict[str, Any]) -> None:
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


def accounting(root: Path) -> dict[str, Any]:
    snapshots = json.loads((root / "snapshots.json").read_text())
    requests = {s["completed"][-1]["rid"] for s in snapshots}
    prefix = root / "prefill-normal.log"
    if not prefix.exists():
        prefix = root / "prefill.log"
    records = log_records(prefix, "ready_accounting")
    if not requests or {r["rid"] for r in records} != requests:
        raise ValueError("missing ready accounting for completed requests")
    if any(r["ready_tokens"] != r["counter_tokens"] for r in records):
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
        equivalent_events=len(records),
        reader_counts_zero=True,
    )


def check_quiesce(snapshot: dict[str, Any]) -> None:
    if (
        not snapshot["stopping"]
        or not snapshot["quiesced"]
        or snapshot["fault"] is not None
        or snapshot["active"] is not None
    ):
        raise ValueError("Main/SR have not completed controlled shutdown")
    for role in ("prefill", "decode"):
        ranks = snapshot["quiesce_acks"][role]
        if [r["rank"] for r in ranks] != list(range(16)) or any(
            r["idle"] is not True for r in ranks
        ):
            raise ValueError("control quiesce lacks every idle TP rank")


def check_normal_close(root: Path) -> None:
    for role in ("prefill", "decode"):
        path = root / f"{role}.log"
        log = path.read_text(errors="replace")
        if any(
            marker in log
            for marker in (
                "Schedulers still alive",
                "Scheduler hit an exception",
                "SR fail-stop:",
                "double free or corruption",
            )
        ):
            raise ValueError(
                f"{role}: normal shutdown used forced cleanup or crashed; see {path}"
            )
        closed = log_records(path, "session_closed")
        if {r["rank"] for r in closed if r["status"] == "passed"} != set(range(16)):
            raise ValueError("normal shutdown lacks all-rank BM close receipts")
        exits = log_records(path, "worker_exit")
        if {
            r["rank"]
            for r in exits
            if r["role"] == role and r["stage"] == "distributed_released"
        } != set(range(16)):
            raise ValueError("normal shutdown lacks all-rank worker cleanup receipts")


def summarize(root: Path) -> dict[str, Any]:
    rounds = []
    for name in SERVICE_STAGES:
        stage = root / name

        def read(path: str) -> Any:
            return json.loads((stage / path).read_text())

        evidence, answers = read("evidence.json"), read("answers.json")
        if (
            evidence["status"] != "passed"
            or evidence["requests"] != 6
            or evidence["prefix"]["status"] != "passed"
        ):
            raise ValueError(f"{name}: incomplete model/content/lifecycle evidence")
        if len(answers) != 6 or any(a["status"] != "passed" for a in answers):
            raise ValueError(f"{name}: logical answers failed")
        rejections = read("input-rejections.json")
        if rejections["status"] != "passed" or not rejections["ledger_unchanged"]:
            raise ValueError(f"{name}: input rejection failed")
        check_registration(read("registrations.json"))
        for filename in (
            "process-cleanup.json",
            "ports-released.json",
            "capacity-replay.json",
        ):
            if read(filename)["status"] != "passed":
                raise ValueError(f"{name}: {filename} failed")
        rounds.append(dict(stage=name, status="passed", accounting=accounting(stage)))
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
    check_quiesce(json.loads((root / "restart/control-quiesced.json").read_text()))
    check_normal_close(root / "restart")
    return dict(
        status="passed",
        coverage="K1-02 reduced functional acceptance",
        service_rounds=len(SERVICE_STAGES),
        rounds=rounds,
        user_accepted=False,
        limitations=[
            "V3 uses independent fixture answers; no fresh formal-service comparison or cold A/C stability run.",
            "V8 capacity rejection replays actual registration on CPU; no isolated worker failure run.",
            "Physical NUMA placement and performance are not accepted by this run.",
            "Fault teardown uses paired process exit and successful restart; no post-exit physical DRAM page count is available.",
        ],
        retained="ABCE, load/EOS, KV content, Graph, source fetch, slot reuse, ready/free accounting, fail-stop, restart and paired cleanup",
    )
