"""Readable, fixed-length ticket04 inputs and independent answer checks."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from itertools import zip_longest
from typing import Any

import verify_mempool_min

POLICY = "mempool-min-logical-2026-10-27-v1"
PAD = "[[ARCHIVE_PADDING]]"
NOTE = "The archive records a routine inspection. No inventory changed.\n"
QUESTION = (
    'Reply with only a JSON object with keys "place", "pencils", "route". '
    "Use the place name, the final pencil count as an integer, and the route. "
    'Choose "north" if the marker is blue; otherwise choose "south".\n'
)


def render(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    return str(
        tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
    )


def padded_messages(
    tokenizer: Any, messages: list[dict[str, str]], length: int
) -> tuple[list[dict[str, str]], str, list[int]]:
    """Fit complete readable sentences and whitespace, never truncate token IDs."""

    def candidate(padding: str) -> tuple[list[dict[str, str]], str, list[int]]:
        filled = [dict(m, content=m["content"].replace(PAD, padding)) for m in messages]
        text = render(tokenizer, filled)
        ids = list(tokenizer.encode(text, add_special_tokens=False))
        return filled, text, ids

    if sum(m["content"].count(PAD) for m in messages) != 1:
        raise ValueError("fixture needs exactly one padding position")
    if len(candidate("")[2]) > length:
        raise ValueError("complete question/template exceeds the required length")
    lo, hi = 0, length
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(candidate(NOTE * mid)[2]) <= length:
            lo = mid
        else:
            hi = mid - 1
    # Newline-terminated units keep padding readable and allow single-token gaps.
    queue = deque(NOTE * count for count in range(lo, max(-1, lo - 3), -1))
    seen: set[int] = set()
    while queue:
        padding = queue.popleft()
        value = candidate(padding)
        count = len(value[2])
        if count == length:
            template_ids = tokenizer.apply_chat_template(
                value[0],
                tokenize=True,
                return_dict=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            if list(template_ids) != value[2]:
                raise ValueError("template tokenization differs from saved input IDs")
            return value
        if count > length or count in seen:
            continue
        seen.add(count)
        queue.extend(
            padding + unit for unit in ("\n", " \n", "Note.\n", "No changes.\n")
        )
    raise ValueError(
        "tokenizer cannot fit an exact length using complete padding units"
    )


def make_cases(tokenizer: Any, page_size: int) -> list[dict[str, Any]]:
    if page_size not in (64, 128):
        raise ValueError("full acceptance requires page_size=64 or 128")
    sampling = dict(
        temperature=0, top_k=1, top_p=1, max_new_tokens=128, ignore_eos=False
    )

    def make(
        name: str, messages: list[dict[str, str]], size: int, expected: dict[str, Any]
    ) -> dict[str, Any]:
        messages, text, ids = padded_messages(tokenizer, messages, size)
        return dict(
            name=name,
            messages=messages,
            rendered_input=text,
            input_ids=ids,
            expected_answer=expected,
            sampling_params=sampling.copy(),
            policy=POLICY,
            template_sha256=hashlib.sha256(
                str(tokenizer.chat_template).encode()
            ).hexdigest(),
        )

    def record(place: str, count: int, given: int, received: int, marker: str) -> str:
        early = f"Place: {place}. Mira initially has {count} pencils.\n"
        middle = (
            "\nMira gave away " + str(given) + f" pencils. The marker is {marker}.\n"
        )
        repetitions = max(
            1, (1900 - len(tokenizer.encode(early))) // len(tokenizer.encode(NOTE))
        )
        return (
            early
            + NOTE * repetitions
            + middle
            + PAD
            + f"\nMira received {received} pencils.\n"
            + QUESTION
        )

    a = make(
        "logic-a",
        [dict(role="user", content=record("harbor", 17, 4, 9, "blue"))],
        4096,
        dict(place="harbor", pencils=22, route="north"),
    )
    update = (
        PAD
        + "\nUpdate the previous record: Mira receives 3 more pencils. The marker is now red.\n"
        + QUESTION
    )
    b = None
    # Templates differ in whether an assistant message includes the no-thinking tag.
    # Every candidate is rendered by the model template, then checked in token space.
    for acknowledgement in ("Ready.", "</think>\nReady.", "\n</think>\nReady."):
        messages = a["messages"] + [
            dict(role="assistant", content=acknowledgement),
            dict(role="user", content=update),
        ]
        candidate = make(
            "logic-b", messages, 5120, dict(place="harbor", pencils=25, route="south")
        )
        if candidate["input_ids"][:4096] == a["input_ids"]:
            b = candidate
            break
    if b is None:
        raise ValueError("model template cannot extend A as an exact token prefix of B")
    c = dict(copy.deepcopy(a), name="logic-c")
    e = make(
        "logic-e",
        [dict(role="user", content=record("meadow", 28, 6, 5, "red"))],
        4096,
        dict(place="meadow", pencils=27, route="south"),
    )
    if e["input_ids"][:page_size] == a["input_ids"][:page_size]:
        raise ValueError("E must differ within its first page")
    return [a, b, c, e]


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate answer field")
    return value


INDEX_CASE_NAMES = ("index-cold", "index-update", "index-full-hit")


def make_index_cases(
    tokenizer: Any, base: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One extra experiment using the retained A prefix and independent answers."""
    cold = (
        "A warehouse has 31 tiles. It ships 8 tiles and receives 6 tiles.\n"
        + PAD
        + '\nReply only with JSON keys "tiles" and "route". Report the final tile count. '
        + 'Choose "west" when the final count is odd, otherwise "east".\n'
    )
    update = (
        "Independent audit of the original harbor record, ignoring any other updates.\n"
        + PAD
        + "\nMira gives away 7 pencils and then receives 2 pencils. The marker is blue.\n"
        + QUESTION
    )
    cases = []
    for name, messages, size, expected in (
        (
            INDEX_CASE_NAMES[0],
            [dict(role="user", content=cold)],
            4096,
            dict(tiles=29, route="west"),
        ),
        (
            INDEX_CASE_NAMES[1],
            base[0]["messages"]
            + [base[1]["messages"][-2], dict(role="user", content=update)],
            5120,
            dict(place="harbor", pencils=17, route="north"),
        ),
    ):
        messages, text, ids = padded_messages(tokenizer, messages, size)
        cases.append(
            dict(
                copy.deepcopy(base[0]),
                name=name,
                messages=messages,
                rendered_input=text,
                input_ids=ids,
                expected_answer=expected,
            )
        )
    if (
        cases[0]["input_ids"][:128] == base[0]["input_ids"][:128]
        or cases[1]["input_ids"][:4096] != base[0]["input_ids"]
        or cases[1]["input_ids"][4096:4224] == base[1]["input_ids"][4096:4224]
    ):
        raise ValueError(
            "Index K logic experiment lacks its cold/4096-token hit boundaries"
        )
    cases.append(dict(copy.deepcopy(base[0]), name=INDEX_CASE_NAMES[2]))
    return cases


def check_index_accuracy(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """A logical failure is evidence to investigate, never permission to read BM."""
    if (
        len(cases) != 9
        or len(results) != 9
        or len(snapshots) != 9
        or tuple(c["name"] for c in cases[6:]) != INDEX_CASE_NAMES
    ):
        raise ValueError(
            "Index K accuracy experiment requires its complete nine-request sequence"
        )
    checks = []
    for case, result, snapshot, hit in zip(
        cases[6:], results[6:], snapshots[6:], (0, 4096, 3968)
    ):
        checked = answer(case, result)
        actual_hit = snapshot["completed"][-1]["hit_len"]
        if actual_hit != hit:
            checked = dict(
                status="failed", category="reuse", error="unexpected prefix hit length"
            )
        checks.append(
            dict(
                checked,
                case=case["name"],
                hit_len=actual_hit,
                expected=case["expected_answer"],
            )
        )
    return dict(
        status="passed" if all(c["status"] == "passed" for c in checks) else "failed",
        cases=checks,
        bm_readback=False,
        cold_vs_full_hit_ids=id_difference(results[0], results[8]),
        failure_action="preserve outputs and discuss diagnosis; no automatic BM readback",
    )


def make_controls(
    tokenizer: Any, page_size: int, decode_capacity: int = 128
) -> list[dict[str, Any]]:
    cases = []
    for name, text, length, ignore_eos, expected in (
        (
            "long-generation",
            "List the integers from 1 to 200 in increasing order, one per line.\n",
            4096,
            True,
            None,
        ),
        (
            "early-eos",
            'Compute 2 + 3. Reply only with a JSON object with key "sum".\n',
            128,
            False,
            dict(sum=5),
        ),
    ):
        messages, rendered, ids = padded_messages(
            tokenizer, [dict(role="user", content=text + PAD)], length
        )
        cases.append(
            dict(
                name=name,
                messages=messages,
                rendered_input=rendered,
                input_ids=ids,
                sampling_params=dict(
                    temperature=0,
                    top_k=1,
                    top_p=1,
                    max_new_tokens=decode_capacity if ignore_eos else 128,
                    ignore_eos=ignore_eos,
                ),
                kind="load-only" if ignore_eos else "logical",
                expected_answer=expected,
                policy=POLICY,
            )
        )
    return cases


def answer(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Keep malformed transport evidence separate from a wrong logical answer."""
    try:
        verify_mempool_min.check_response(case, result)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return dict(status="failed", category="structure", error=str(exc))
    if case.get("kind") == "load-only":
        return dict(
            status="passed",
            coverage=f"{case['sampling_params']['max_new_tokens']} output tokens; load only, no answer acceptance",
        )
    try:
        parsed = json.loads(result["text"], object_pairs_hook=unique_fields)
        expected = case["expected_answer"]
        if parsed != expected or any(
            type(parsed[k]) is not type(v) for k, v in expected.items()
        ):
            raise ValueError("answer differs from independent fixture")
    except (ValueError, KeyError, TypeError) as exc:
        return dict(status="failed", category="answer", error=str(exc))
    return dict(status="passed", answer=parsed)


def id_difference(reference: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    left, right = reference.get("output_ids"), target.get("output_ids")
    left = left if isinstance(left, list) else []
    right = right if isinstance(right, list) else []
    first = next(
        (i for i, (a, b) in enumerate(zip_longest(left, right)) if a != b), None
    )
    return dict(
        equal=left == right,
        reference_length=len(left),
        target_length=len(right),
        first_difference=first,
    )


def check_stability(
    cases: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    if (
        len(cases) < 3
        or len(results) < 3
        or any(
            cases[0][key] != cases[2][key] for key in ("input_ids", "sampling_params")
        )
    ):
        raise ValueError(
            "stability diagnostic requires identical cold A/C inputs and sampling"
        )
    for index in (0, 2):
        verify_mempool_min.check_response(cases[index], results[index])
    ids = id_difference(results[0], results[2])
    return dict(
        status="passed" if ids["equal"] else "anomaly",
        ids=ids,
        comparison="reference A versus reference C; same conditions",
        action="none"
        if ids["equal"]
        else "analyze cold nondeterminism; do not waive as cross-shape reuse difference",
    )


def compare_answers(
    case: dict[str, Any], reference: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    baseline, actual = answer(case, reference), answer(case, target)
    status = "passed"
    if baseline["status"] != "passed":
        status = "baseline-failed"
    elif actual["status"] != "passed":
        status = "target-failed"
    return dict(
        status=status,
        policy=POLICY,
        case=case["name"],
        reference=baseline,
        target=actual,
        ids=id_difference(reference, target),
    )
