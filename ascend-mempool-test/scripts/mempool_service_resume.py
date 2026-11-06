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
    if old_runtime != new_runtime:
        raise ValueError("resume requires matching runtime/config/model identity")
    summary = json.loads((previous / "summary.json").read_text())
    stages = ("reference",) if through == "reference" else ("reference", "target")
    if any(stage not in summary.get("stages", []) for stage in stages):
        raise ValueError(f"resume requires completed stages: {stages}")
    receipts = {f"{stage}/process-cleanup.json": "passed" for stage in stages}
    receipts.update(
        {"page-first-copy/report.json": "passed", "fetch/report.json": "passed"}
    )
    if role == "prefill":
        receipts["reference/evidence.json"] = "formal_service_passed"
        if through == "target":
            receipts["target/evidence.json"] = "passed"
    for name, expected in receipts.items():
        if json.loads((previous / name).read_text()).get("status") != expected:
            raise ValueError(f"resume requires passed evidence: {name}")
    cases = json.loads((previous / "cases.json").read_text())
    for stage in ("page-first-copy", "fetch", *stages):
        shutil.copytree(previous / stage, current / stage)
    shutil.copyfile(previous / "cases.json", current / "cases.json")
    (current / "service-reused-from.json").write_text(
        json.dumps(
            dict(
                path=str(previous),
                run_id=prior["run_id"],
                stages=list(stages),
                components=["page-first-copy", "fetch"],
                identity=prior,
                runtime_identity_checked=True,
                next_stage="target" if through == "reference" else "capacity",
            ),
            indent=2,
        )
        + "\n"
    )
    return cases
