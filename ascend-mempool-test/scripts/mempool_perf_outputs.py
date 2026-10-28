"""Offline output evidence for forced-length perf requests (standard library only)."""

from __future__ import annotations

import json
from itertools import zip_longest
from typing import Any

OUTPUT_POLICY = "core-answer-v1"
STRICT_POLICY = "strict-ids-v1"


def id_difference(reference: dict, actual: dict) -> dict:
    left = reference.get("output_ids") if isinstance(reference, dict) else None
    right = actual.get("output_ids") if isinstance(actual, dict) else None
    comparable = all(
        isinstance(ids, list) and all(type(token) is int for token in ids)
        for ids in (left, right)
    )
    if not comparable:
        return dict(equal=None, first_difference=None, error="missing/malformed IDs")
    first = next(
        (i for i, (a, b) in enumerate(zip_longest(left, right)) if a != b), None
    )
    return dict(
        equal=left == right,
        reference_length=len(left),
        actual_length=len(right),
        first_difference=first,
        reference_token=left[first]
        if first is not None and first < len(left)
        else None,
        actual_token=right[first] if first is not None and first < len(right) else None,
    )


def core_answer(fixture: dict, response: dict) -> dict:
    """Grade only the first JSON object; never search a continuation for a good answer."""
    result: dict[str, Any] = dict(
        status="failed", expected=fixture.get("expected_answer")
    )

    def unique_fields(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate answer field")
        return value

    try:
        expected = fixture["expected_answer"]
        if (
            not isinstance(expected, dict)
            or set(expected) != {"place", "pencils", "route"}
            or type(expected["pencils"]) is not int
            or not all(type(expected[k]) is str for k in ("place", "route"))
        ):
            raise ValueError("missing/malformed independent expected answer")
        text = response["text"]
        if not isinstance(text, str):
            raise ValueError("missing/malformed output text")
        # Preserve the exact tail, including spaces. Optional fences are not
        # stripped: the fixture asks for a JSON object at the start of the answer.
        offset = len(text) - len(text.lstrip())
        parsed, end = json.JSONDecoder(object_pairs_hook=unique_fields).raw_decode(
            text, offset
        )
        result.update(answer=parsed, answer_end=end, continuation=text[end:])
        if (
            not isinstance(parsed, dict)
            or parsed != expected
            or any(type(parsed[k]) is not type(v) for k, v in expected.items())
        ):
            raise ValueError("core answer differs from independent fixture")
        result["status"] = "passed"
    except (ValueError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
    return result


def compare_outputs(fixture: dict, reference: dict, actual: dict) -> dict:
    baseline, target = core_answer(fixture, reference), core_answer(fixture, actual)
    return dict(
        policy=OUTPUT_POLICY,
        ids=id_difference(reference, actual),
        reference=baseline,
        actual=target,
        errors=[
            f"{label} logical answer: {answer['error']}"
            for label, answer in (("reference", baseline), ("actual", target))
            if answer["status"] != "passed"
        ],
    )


def output_analysis(
    cases: list[dict], references: list[dict], records: list[dict]
) -> dict:
    """Keep full outputs and compare all rounds, including warmup/invalid samples."""
    analysis = dict(
        schema=1,
        policy=OUTPUT_POLICY,
        user_accepted=False,
        first_difference_index_base=0,
        stability_includes_warmup_and_invalid=True,
        references=[
            dict(case=case, result=response, logical=core_answer(fixture, response))
            for case, fixture, response in zip(("A", "B", "C", "E"), cases, references)
        ],
        reference_A_vs_C=id_difference(references[0], references[2]),
        requests=[],
        stability=[],
    )
    for case in ("A", "B", "C", "E"):
        saved = [record for record in records if record["case"] == case]
        available = [
            record for record in saved if isinstance(record.get("result"), dict)
        ]
        baseline = available[0] if available else None
        groups: dict[tuple, list[int]] = {}
        logical_groups: dict[str, list[int]] = {}
        for record in available:
            output = record["result"]
            comparison = id_difference(baseline["result"], output)
            if comparison["equal"] is not None:
                groups.setdefault(tuple(output["output_ids"]), []).append(
                    record["round"]
                )
            logical = record.get("output_diagnostics", {}).get("actual", {})
            if "answer" in logical:
                key = json.dumps(logical["answer"], sort_keys=True, ensure_ascii=False)
                logical_groups.setdefault(key, []).append(record["round"])
            analysis["requests"].append(
                dict(
                    **{k: v for k, v in record.items() if k != "snapshot"},
                    versus_first_actual=dict(round=baseline["round"], **comparison),
                )
            )
        analysis["stability"].append(
            dict(
                case=case,
                observed_rounds=[r["round"] for r in available],
                missing_rounds=[
                    r["round"] for r in saved if not isinstance(r.get("result"), dict)
                ],
                id_groups=list(groups.values()),
                all_ids_equal=len(groups) == 1
                if sum(map(len, groups.values())) == len(available)
                and len(available) >= 2
                else None,
                core_answer_groups=[
                    dict(answer=json.loads(answer), rounds=rounds)
                    for answer, rounds in logical_groups.items()
                ],
                logical_failures=[
                    r["round"]
                    for r in available
                    if r.get("output_diagnostics", {}).get("actual", {}).get("status")
                    != "passed"
                ],
            )
        )
    return analysis
