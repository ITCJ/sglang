"""Offline archive contracts: no hardware or service imports; synthetic clocks.

These cases guard false acceptance from pooled steps, warmup inclusion, clock
mixing, missing receipts, and an invalid prefix-preparation request.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import mempool_perf_report as report


def event(name, rid, ms, process="main", rank=0, **data):
    # Deliberately unrelated origins on every process; only durations can reduce.
    return dict(
        schema=1,
        event=name,
        rid=rid,
        mono_ns=int(ms * 1e6) + (rank + (100 if process == "decode" else 1)) * 10**12,
        process=process,
        rank=rank,
        host="d" if process == "decode" else "p",
        pid=100 + rank + (100 if process == "decode" else 0),
        **data,
    )


def request_events(rid, case, warmup=False):
    events = [
        event(name, rid, ms, **data)
        for name, ms, data in (
            ("request", 0, {}),
            ("dispatch_send", 10, dict(role="decode", op_id=1)),
            ("ready_tokens", 100, {}),
            ("finished", 1000, {}),
            ("publish", 1010, {}),
            ("free_send", 1011, dict(role="prefill", op_id=2)),
            ("free_ack", 1015, dict(role="prefill", op_id=2)),
            ("free_send", 1012, dict(role="decode", op_id=3)),
            ("free_ack", 1018, dict(role="decode", op_id=3)),
            ("response_sent", 10000 if warmup else 1020, {}),
        )
    ]
    if case in ("B", "C"):
        for op, start, end, op_id in (("load_back", 20, 30, 4), ("fill", 40, 80, 5)):
            events += [
                event(op + "_send", rid, start, op_id=op_id),
                event(op + "_ack", rid, end, op_id=op_id),
            ]
    for rank in range(16):
        events += [
            event(name, rid, ms, "prefill", rank, **data)
            for name, ms, data in (
                ("forward_begin", 20, dict(step=1)),
                ("forward_end", 40 + rank, dict(step=1)),
                ("write_complete", 35 + rank, dict(step=1)),
                ("send_begin", 30, {}),
                ("handoff_complete", 60, {}),
                ("hbm_release", 70, {}),
            )
        ]
        if case in ("B", "C"):
            events += [
                event("load_back_begin", rid, 1, "prefill", rank),
                event("load_back_end", rid, 10 + rank, "prefill", rank),
            ]
    events += [
        event(name, rid, ms, "decode", **data)
        for name, ms, data in (
            ("dispatch_received", 5, {}),
            ("prompt_ready", 12, {}),
            ("index_ready", 10, {}),
            ("handoff_ready", 13, {}),
            ("gate_ready", 15, {}),
            ("first_token_sent", 350, dict(output_tokens=50)),
            ("finished_send", 930, {}),
        )
    ]
    for step in range(1, 128):
        events += [
            event("forward_begin", rid, 20 + 7 * step, "decode", step=step),
            event("forward_end", rid, 25 + 7 * step, "decode", step=step),
        ]
    return events


@pytest.fixture
def archive(tmp_path):
    cases = [
        dict(
            input_ids=[7] * size,
            sampling_params=dict(temperature=0, ignore_eos=True, max_new_tokens=128),
        )
        for size in (4096, 5120, 4096, 4096)
    ]
    cases[3]["input_ids"] = [8] * 4096
    responses = [
        dict(
            output_ids=list(range(128)),
            meta_info=dict(
                prompt_tokens=len(case["input_ids"]),
                completion_tokens=128,
                finish_reason=dict(type="length"),
                e2e_latency=1.0,
            ),
        )
        for case in cases
    ]
    report.write_json(tmp_path / "cases.json", cases)
    report.write_json(
        tmp_path / "reference/responses.json", dict(cases=cases, results=responses)
    )
    report.write_json(tmp_path / "perf-manifest.json", dict(rounds=5))
    report.write_json(
        tmp_path / "effective-config.json",
        dict(
            perf=True,
            prefix_pages=40,
            verify_prefix=False,
            match_prefix=True,
            page_size=128,
            prompt_capacity=8192,
            decode_capacity=128,
        ),
    )
    for name in ("identity.json", "topology.json", "decode-topology.json"):
        report.write_json(tmp_path / name, dict(synthetic=True))
    for number in range(1, 6):
        folder = tmp_path / f"round-{number:02d}"
        folder.mkdir()
        records, events = [], []
        for index, case in enumerate(report.CASES):
            response = responses[index]
            rid = f"r{number}-{case}"
            size = 32 if case == "A" else 40
            directory = {
                str(i): dict(
                    owner=None,
                    lock_ref=0,
                    source_ops=[],
                    prefix=dict(state="ready", lock_ops=[], locations=[]),
                )
                for i in range(size)
            }
            snapshot = dict(
                fault=None,
                active=None,
                completed=[
                    dict(
                        rid=rid,
                        result=response,
                        hit_len=dict(A=0, B=4096, C=3968, E=0)[case],
                    )
                ],
                directory=directory,
                ledger=dict(
                    p_alive=dict(total=64, free=64, outstanding=0),
                    d_alive=dict(total=1, free=1, outstanding=0),
                    d_hbm=dict(total=128, free=128, outstanding=0),
                    d_rows=dict(total=16, free=16, outstanding=0),
                    p_prefix=dict(total=40, free=40 - size, outstanding=size),
                ),
            )
            records.append(dict(case=case, rid=rid, result=response, snapshot=snapshot))
            events += request_events(rid, case, number == 1)
        report.write_json(
            folder / "outcome.json",
            dict(
                initial=dict(
                    directory={},
                    ledger=dict(p_prefix=dict(outstanding=0)),
                    registrations={
                        role: dict(
                            ranks=[
                                dict(
                                    rank=i,
                                    numa_node=i // 2,
                                    numa_placement_verified=False,
                                )
                                for i in range(16)
                            ]
                        )
                        for role in ("prefill", "decode")
                    },
                ),
                samples=records,
                paired_cleanup=True,
            ),
        )
        (folder / "router.log").write_text(
            "".join("mempool_perf data=" + json.dumps(e) + "\n" for e in events)
        )
    return tmp_path


def rewrite_events(root, number, change):
    path = root / f"round-{number:02d}/router.log"
    events = [
        json.loads(line.split("data=", 1)[1]) for line in path.read_text().splitlines()
    ]
    path.write_text(
        "".join("mempool_perf data=" + json.dumps(e) + "\n" for e in change(events))
    )


def test_report_excludes_warmup_reduces_local_rank_durations_and_keeps_every_step(
    archive,
):
    result = report.build_report(archive)
    assert result["status"] == "passed", result["failures"]
    rows = {(r["metric"], r["case"]): r for r in result["table"]}
    assert rows["T_main_e2e", "A"]["p50"] == 1020
    assert rows["T_prefill_min", "A"]["p50"] == 20
    assert rows["T_prefill_max", "A"]["p50"] == 35
    assert rows["T_step", "B"]["n"] == 508
    assert rows["T_step", "B"]["request_n"] == 4
    assert result["requests"][0]["actual_steps"] == 127
    assert result["requests"][0]["first_emission_tokens"] == 50
    raw = [json.loads(s) for s in (archive / "samples.jsonl").read_text().splitlines()]
    assert len([s for s in raw if s["metric"] == "T_step"]) == 5 * 4 * 127
    assert "2.000" in (archive / "baseline.md").read_text()  # residual


@pytest.mark.parametrize("defect", ["host", "pid", "missing_step", "duplicate_step"])
def test_clock_or_step_corruption_invalidates_request_and_dependent_cases(
    archive, defect
):
    def corrupt(events):
        target = next(
            e
            for e in events
            if e["rid"] == "r3-B"
            and e["process"] == "decode"
            and e["event"] == "forward_end"
            and e["step"] == 8
        )
        if defect in ("host", "pid"):
            target[defect] = "different process"
        elif defect == "missing_step":
            events.remove(target)
        else:
            events.append(copy.deepcopy(target))
        return events

    rewrite_events(archive, 3, corrupt)
    result = report.build_report(archive)
    assert result["status"] == "failed"
    assert result["invalid_samples"] == 3
    assert all(
        not r["valid"]
        for r in result["requests"]
        if r["round"] == 3 and r["case"] in ("B", "C", "E")
    )
    assert (
        next(
            r for r in result["table"] if r["metric"] == "T_step" and r["case"] == "B"
        )["request_n"]
        == 3
    )


def test_strict_ids_and_ledger_cannot_be_waived_as_semantic_accuracy(archive):
    path = archive / "round-02/outcome.json"
    outcome = report.read_json(path)
    outcome["samples"][1]["result"]["output_ids"][30] = 999
    report.write_json(path, outcome)
    result = report.build_report(archive)
    assert result["status"] == "failed"
    assert result["invalid_samples"] == 3
    assert "V3" in result["requests"][5]["error"]
    with pytest.raises(ValueError, match="ledger"):
        sample = report.read_json(archive / "round-03/outcome.json")["samples"][0]
        sample["snapshot"]["ledger"]["p_alive"]["free"] -= 1
        report.check_sample(
            "A",
            report.read_json(archive / "cases.json")[0],
            sample["result"],
            sample["result"],
            sample["snapshot"],
            {},
        )


def test_unexplained_cross_round_instability_fails_until_evidence_is_recorded(archive):
    def spike(events):
        for e in events:
            if e["rid"] == "r5-A" and e["event"] == "response_sent":
                e["mono_ns"] += 3000 * 10**6
        return events

    rewrite_events(archive, 5, spike)
    result = report.build_report(archive)
    assert result["status"] == "failed"
    assert result["unstable"][0]["key"] == "T_main_e2e/A"
    result = report.build_report(
        archive,
        {"T_main_e2e/A": "Synthetic test deliberately delays response_sent by 3000ms."},
    )
    assert result["status"] == "passed"


def test_process_cleanup_is_required_for_valid_samples(archive):
    path = archive / "round-04/outcome.json"
    outcome = report.read_json(path)
    outcome["paired_cleanup"] = False
    report.write_json(path, outcome)
    assert report.main([str(archive)]) == 1
    assert report.read_json(archive / "perf-report.json")["invalid_samples"] == 4


def test_invalid_sample_keeps_measured_values_and_missing_evidence_fails(archive):
    path = archive / "round-02/outcome.json"
    outcome = report.read_json(path)
    outcome["samples"][0]["snapshot"]["ledger"].pop("d_rows")
    report.write_json(path, outcome)
    result = report.build_report(archive)
    assert result["status"] == "failed"
    raw = [
        json.loads(line)
        for line in (archive / "samples.jsonl").read_text().splitlines()
    ]
    bad = [row for row in raw if row["rid"] == "r2-A" and row["metric"] == "T_main_e2e"]
    assert len(bad) == 1 and bad[0]["value_ms"] == 1020 and not bad[0]["valid"]
    (archive / "topology.json").unlink()
    result = report.build_report(archive)
    assert any("topology.json" in error for error in result["failures"])


def test_gate_and_rank_registration_cannot_precede_their_required_facts(archive):
    def early_gate(events):
        target = next(
            e for e in events if e["rid"] == "r2-A" and e["event"] == "gate_ready"
        )
        target["mono_ns"] -= 4 * 10**6
        return events

    rewrite_events(archive, 2, early_gate)
    result = report.build_report(archive)
    assert result["status"] == "failed"
    assert "negative event interval" in result["requests"][4]["error"]
    path = archive / "round-03/outcome.json"
    outcome = report.read_json(path)
    outcome["initial"]["registrations"]["prefill"]["ranks"].pop()
    report.write_json(path, outcome)
    result = report.build_report(archive)
    assert "rank-to-NUMA" in result["requests"][8]["error"]


def test_early_failure_writes_failed_diagnostic_report(tmp_path):
    assert report.main([str(tmp_path)]) == 1
    assert "未形成有效基线" in (tmp_path / "baseline.md").read_text()
    assert report.read_json(tmp_path / "perf-report.json")["status"] == "failed"


def test_paired_round_failure_cleans_owned_processes_and_next_round_restarts(
    archive, tmp_path, monkeypatch
):
    """Use real paired coordination/owned processes, replacing NPU and HTTP edges."""
    import socket
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "src"))
    import mempool_perf_runner as run
    from ticket10_processes import Peer, Processes

    from ascend_mempool_pd.mempool_router_protocol import RouterConfig

    started = []
    start = Processes.start

    def cpu_process(self, name, command, **environment):
        self.root.mkdir(parents=True, exist_ok=True)
        if name == "launcher":
            role = self.root.parent.name
            for suffix in (
                ".log",
                "-environment.txt",
                "-model-config.json",
                "-quant-model-description.json",
            ):
                (self.root / (role + suffix)).write_text("{}\n")
        process = start(
            self,
            name,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **environment,
        )
        started.append(process)
        return process

    monkeypatch.setattr(Processes, "start", cpu_process)
    monkeypatch.setattr(run, "wait_http", lambda url, timeout, watch: watch())
    config = RouterConfig(
        "p", "d", "/model", prefix_pages=40, match_prefix=True, perf=True
    )
    left, right = socket.socketpair()
    with (
        Peer(left, 10) as peer_p,
        Peer(right, 10) as peer_d,
        ThreadPoolExecutor(2) as workers,
    ):
        runners = [
            SimpleNamespace(
                args=SimpleNamespace(
                    role=role, run_id="cpu", config=tmp_path / "config.json"
                ),
                config=config,
                peer=peer,
                root=tmp_path / "paired" / role,
                is_p=role == "prefill",
                report=dict(stages=[]),
                cases=report.read_json(archive / "cases.json"),
                reference=report.read_json(archive / "reference/responses.json")[
                    "results"
                ],
            )
            for role, peer in (("prefill", peer_p), ("decode", peer_d))
        ]
        for number in (1, 2):
            data = report.read_json(archive / f"round-{number:02d}/outcome.json")
            calls = []

            def http(url, *, payload=None, timeout=10):
                if payload is not None:
                    index = len(calls)
                    calls.append(payload)
                    response = copy.deepcopy(data["samples"][index]["result"])
                    if number == 1 and index == 1:
                        response["output_ids"] = response["output_ids"][:-1]
                    return response
                return (
                    data["samples"][len(calls) - 1]["snapshot"]
                    if calls
                    else data["initial"]
                )

            monkeypatch.setattr(run, "request_json", http)
            futures = [
                workers.submit(run.run_round, runner, number) for runner in runners
            ]
            for task in futures:
                task.result(timeout=15)
            p_outcome = report.read_json(
                runners[0].root / f"round-{number:02d}/outcome.json"
            )
            d_outcome = report.read_json(
                runners[1].root / f"round-{number:02d}/outcome.json"
            )
            assert p_outcome == d_outcome and p_outcome["paired_cleanup"]
            assert [sample["valid"] for sample in p_outcome["samples"]] == (
                [True, False, False, False] if number == 1 else [True] * 4
            )
            assert len(calls) == (2 if number == 1 else 4)
            assert all(process.poll() is not None for process in started)
        assert len({process.pid for process in started}) == 6
