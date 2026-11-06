"""Reuse completed service evidence without repeating passed model runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

REPO = Path(__file__).resolve().parents[2]


def runtime_identity(root: Path, identity: dict[str, Any]) -> dict[str, Any]:
    # Test-runner fixes may change; all deployed Python/kernel sources must match.
    patch = (root / "code.patch").read_text()
    return dict(
        **{key: identity[key] for key in ("git_sha", "config", "model", "versions")},
        runtime_patch=[
            section
            for section in patch.split("diff --git ")
            if section.startswith("a/python/")
        ],
        untracked_sha256={
            name: sha
            for name, sha in identity["untracked_sha256"].items()
            if name.startswith("python/")
        },
    )


def reuse_completed_stages(
    previous: Path,
    current: Path,
    identity: dict[str, Any],
    role: str,
    *,
    through: Literal["reference", "target"] = "target",
    include_components: bool = True,
    service_timeout: float | None = None,
) -> list[dict[str, Any]]:
    previous = previous.resolve()
    if previous.name != role:
        raise ValueError("resume evidence belongs to a different role")
    prior = json.loads((previous / "identity.json").read_text())
    old_runtime = runtime_identity(previous, prior)
    new_runtime = runtime_identity(current, identity)
    if prior["git_sha"] != identity["git_sha"]:
        # A runner-only commit changes HEAD, but not deployed Python/kernel bytes.
        # Resolve the archived commit too, so old reports need no new identity field.
        for value in (old_runtime, new_runtime):
            value["git_sha"] = subprocess.check_output(
                ["git", "rev-parse", "--verify", f"{value['git_sha']}:python"],
                cwd=REPO,
                text=True,
            ).strip()
    timeout_change = None
    if service_timeout is not None:
        old_timeout = old_runtime["config"]["operation_timeout"]
        if (
            include_components
            or through != "reference"
            or new_runtime["config"]["operation_timeout"] != service_timeout
            or service_timeout <= old_timeout
        ):
            raise ValueError(
                "service timeout override must only increase the reference deadline"
            )
        timeout_change = dict(reference=old_timeout, target=service_timeout)
        old_runtime["config"] = dict(
            old_runtime["config"], operation_timeout=service_timeout
        )
    if old_runtime != new_runtime:
        raise ValueError("resume requires matching runtime/config/model identity")
    summary = json.loads((previous / "summary.json").read_text())
    if not include_components and (
        through != "reference"
        or summary.get("status") != "passed_execution_scope"
        or summary.get("stopped_after") != "reference"
        or summary.get("stages") != ["reference"]
    ):
        raise ValueError(
            "service-only resume requires a successful reference-only report"
        )
    components = ("page-first-copy", "fetch") if include_components else ()
    stages = ("reference",) if through == "reference" else ("reference", "target")
    if any(stage not in summary.get("stages", []) for stage in stages):
        raise ValueError(f"resume requires completed stages: {stages}")
    receipts = {f"{stage}/process-cleanup.json": "passed" for stage in stages}
    receipts.update({f"{stage}/report.json": "passed" for stage in components})
    if role == "prefill":
        receipts["reference/evidence.json"] = "formal_service_passed"
        if through == "target":
            receipts["target/evidence.json"] = "passed"
    for name, expected in receipts.items():
        if json.loads((previous / name).read_text()).get("status") != expected:
            raise ValueError(f"resume requires passed evidence: {name}")
    cases = json.loads((previous / "cases.json").read_text())
    for stage in (*components, *stages):
        shutil.copytree(previous / stage, current / stage)
    shutil.copyfile(previous / "cases.json", current / "cases.json")
    (current / "service-reused-from.json").write_text(
        json.dumps(
            dict(
                path=str(previous),
                run_id=prior["run_id"],
                stages=list(stages),
                components=list(components),
                identity=prior,
                runtime_identity_checked=True,
                operation_timeout_override=timeout_change,
                next_stage="target" if through == "reference" else "capacity",
            ),
            indent=2,
        )
        + "\n"
    )
    return cases


def check_service_answers(root: Path, *, target: bool, case_count: int) -> None:
    """Apply the answer gates before a single service stage can report success."""
    answers = json.loads((root / "answers.json").read_text())
    if (
        len(answers) != case_count
        or not answers
        or any(item.get("status") != "passed" for item in answers)
    ):
        raise ValueError("single service stage has failed or incomplete answers")
    if target:
        if any(not item.get("ids", {}).get("equal") for item in answers):
            raise ValueError("full output IDs differ from formal reference")
    elif json.loads((root / "stability.json").read_text()).get("status") != "passed":
        raise ValueError("reference stability failed; preserve outputs for diagnosis")
