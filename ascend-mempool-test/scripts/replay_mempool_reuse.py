"""Validate saved ABC payloads and deployment identity before a diagnostic replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PURPOSE = "diagnostic comparison; not full acceptance"
CONTROLS = ("force_noncontiguous_restore", "delay_prefix_fill")


def validate_cases(cases: list[dict[str, Any]]) -> None:
    if [c["name"] for c in cases] != ["reuse-prefix-" + c for c in "abc"]:
        raise ValueError("saved requests must preserve the original ABC order")
    a, b, c = [case["input_ids"] for case in cases]
    if (
        list(map(len, (a, b, c))) != [4096, 5120, 4096]
        or a != b[:4096]
        or a != c
        or any(
            type(token) is not int or token < 0 for ids in (a, b, c) for token in ids
        )
    ):
        raise ValueError("saved requests do not contain the original ABC token layout")
    for case in cases:
        sampling = case["sampling_params"]
        if (
            sampling.get("ignore_eos") is not True
            or sampling.get("max_new_tokens") != 128
            or sampling.get("temperature") != 0
            or sampling.get("top_k") != 1
            or sampling.get("top_p") != 1
            or sampling != cases[0]["sampling_params"]
        ):
            raise ValueError(
                "saved sampling must retain greedy fixed 128 tokens and ignore_eos=true"
            )


def validate_identity(source: dict[str, Any], current: dict[str, Any]) -> None:
    # Code necessarily changes to split the controls. Everything else affecting
    # this comparison must match the original recorded deployment.
    if source["mode"] not in ("reuse-acceptance", "reuse-diagnostic"):
        raise ValueError("replay source is not an ABC reuse run")
    if (
        source.get("diagnose_reuse") is not True
        or current.get("diagnose_reuse") is not True
    ):
        raise ValueError("both runs must enable the same reuse probes")
    for key in ("model", "versions", "config"):
        left, right = source[key], current[key]
        if key == "config":
            left = {k: v for k, v in left.items() if k not in CONTROLS}
            right = {k: v for k, v in right.items() if k not in CONTROLS}
        if left != right:
            raise ValueError(f"replay {key} differs from saved deployment")
    config = source["config"]
    if not (
        config["verify_prefix"]
        and config["match_prefix"]
        and config["prefix_pages"] == 40
    ):
        raise ValueError(
            "replay requires the original content-verified prefix capacity"
        )


def load_source(root: Path, role: str) -> dict[str, Any]:
    hashes: dict[str, str] = {}

    def read(relative: str) -> Any:
        data = (root / role / relative).read_bytes()
        hashes[role + "/" + relative] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    identity = read("identity.json")
    if identity["run_id"] != root.name:
        raise ValueError("saved identity does not match --replay-run")
    cases = None
    if role == "prefill":
        cases = read("cases.json")
        validate_cases(cases)
        for stage in ("reference", "target"):
            if read(stage + "/responses.json")["cases"] != cases:
                raise ValueError("saved requests differ between cases and responses")
    return dict(identity=identity, cases=cases, files_sha256=hashes)


def payload_digest(cases: list[dict[str, Any]]) -> str:
    payloads = [{k: c[k] for k in ("input_ids", "sampling_params")} for c in cases]
    return hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()


def compare_outputs(
    cases: list[dict[str, Any]],
    reference: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not len(cases) == len(reference) == len(actual) == 3:
        raise ValueError("output comparison requires all three ABC responses")
    comparisons = []
    for case, left, right in zip(cases, reference, actual):
        a, c = left["output_ids"], right["output_ids"]
        first = next(
            (i for i, pair in enumerate(zip(a, c)) if pair[0] != pair[1]), None
        )
        if first is None and len(a) != len(c):
            first = min(len(a), len(c))
        comparisons.append(
            dict(
                name=case["name"],
                equal=a == c,
                reference_length=len(a),
                actual_length=len(c),
                first_difference=first,
            )
        )
    return comparisons
