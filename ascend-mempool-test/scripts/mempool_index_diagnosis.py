"""Replay the saved failing Index K question without prefix reuse, once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import mempool_logical_cases
import verify_mempool_min
import verify_mempool_session
from run_ticket10 import receive_log, send_log

if TYPE_CHECKING:
    from run_mempool_min import Runner


def compare(
    case: dict[str, Any],
    original: dict[str, Any],
    cold: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    done = snapshot["completed"][-1]
    if snapshot["fault"] or snapshot["active"] is not None:
        raise ValueError("cold diagnosis request did not finish cleanly")
    if done["hit_len"] != 0 or done["reads"]:
        raise ValueError("cold diagnosis unexpectedly reused a prefix")
    checked = mempool_logical_cases.answer(case, cold)
    return dict(
        status=checked["status"],
        coverage="one saved input; not ticket acceptance or a formal-service baseline",
        case=case["name"],
        expected=case["expected_answer"],
        original=mempool_logical_cases.answer(case, original),
        cold=checked,
        original_text=original["text"],
        cold_text=cold["text"],
        ids=mempool_logical_cases.id_difference(original, cold),
        hit_len=done["hit_len"],
        bm_readback=False,
    )


def run_stage(runner: Runner, root: Path, base: str, watch: Callable[[], None]) -> None:
    from run_mempool_min import request_json, write_json

    if not runner.is_p:
        runner.peer.expect("diagnosis-snapshot", check=watch)
        send_log(runner.peer, root / "decode.log")
        runner.peer.expect("diagnosis-done", check=watch)
        return
    watch()
    before = request_json(base + "/status")
    write_json(root / "before.json", before)
    verify_mempool_session.check_registration(
        before["registrations"], config=runner.config
    )
    case = runner.cases[0]
    result = request_json(
        base + "/generate",
        payload={key: case[key] for key in ("input_ids", "sampling_params")},
        timeout=runner.config.operation_timeout,
    )
    write_json(root / "raw-response.json", result)
    result["server_text"] = result.get("text")
    result["text"] = runner.tokenizer.decode(
        result["output_ids"], skip_special_tokens=True
    )
    write_json(root / "responses.json", dict(cases=[case], results=[result]))
    snapshot = request_json(base + "/status")
    write_json(root / "snapshots.json", [snapshot])
    verify_mempool_min.check_response(case, result)
    report = compare(case, runner.reference[0], result, snapshot)
    write_json(root / "index-cold-diagnosis.json", report)
    runner.report["diagnosis"] = report
    runner.peer.send("diagnosis-snapshot")
    receive_log(runner.peer, root / "decode.log")
    runner.peer.send("diagnosis-done")


def run(runner: Runner) -> None:
    from run_mempool_min import write_json

    if runner.is_p:
        from transformers import AutoTokenizer

        source = json.loads(runner.args.replay_responses.read_text())
        index = next(
            i
            for i, case in enumerate(source["cases"])
            if case["name"] == "index-update"
        )
        runner.cases = [source["cases"][index]]
        runner.reference = [source["results"][index]]
        write_json(
            runner.root / "source.json",
            dict(cases=runner.cases, results=runner.reference),
        )
        runner.tokenizer = AutoTokenizer.from_pretrained(
            runner.config.model_path, trust_remote_code=True
        )
    runner.peer.barrier("diagnosis-input-ready")
    runner.stage(target=True)
    # Compare after the normal paired shutdown, even when the answer is wrong.
    if runner.is_p:
        runner.peer.send("diagnosis-result", result=runner.report["diagnosis"])
    else:
        runner.report["diagnosis"] = runner.peer.expect("diagnosis-result")["result"]
    if runner.report["diagnosis"]["status"] != "passed":
        raise ValueError(
            "cold diagnostic answer failed; compare target/index-cold-diagnosis.json; "
            "services cleaned up, no BM readback"
        )
    runner.report.update(status="passed", user_accepted=False)
