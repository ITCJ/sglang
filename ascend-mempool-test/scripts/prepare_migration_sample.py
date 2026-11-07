"""Prepare one isolated ticket26 version and a fixed request, without NPU use."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASELINE = "aa5852624e15df139df7c76bb8d757341b049713"


def sampling_input(cases: list[dict]) -> dict:
    case = cases[0]
    if len(case["input_ids"]) != 4096:
        raise ValueError("use the saved 015 cases.json with its first 4096-token case")
    return dict(
        input_ids=case["input_ids"],
        sampling_params=dict(
            temperature=0, top_k=1, top_p=1, max_new_tokens=32, ignore_eos=True
        ),
    )


def extract_git(commit: str, destination: Path, paths: list[str]) -> None:
    data = subprocess.check_output(["git", "archive", commit, *paths], cwd=REPO)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")


def prepare(variant: str, root: Path, config_path: Path, cases_path: Path) -> dict:
    harness = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
    ).strip()
    source_commit = harness if variant == "phase2" else BASELINE
    source = root / "source"
    root.mkdir(parents=True, exist_ok=True)
    # Fresh checkout preserves both original source and the exact harness delta.
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", str(REPO), str(source)],
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", source_commit], cwd=source, check=True
    )
    archive_hash = None
    if variant == "phase1":
        archive_path = (
            REPO / ".scratch/k2/verification/ticket26-local/phase1-source.tar.gz"
        )
        archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        with tarfile.open(archive_path) as archive:
            archive.extractall(source, filter="data")
    # Same current preparation/preflight/cleanup tooling on all three versions;
    # production python/ stays at the selected version, never overlaid here.
    extract_git(
        harness, source, ["ascend-mempool-test/scripts", "ascend-mempool-test/src"]
    )
    config = json.loads(config_path.read_text())
    config.update(observe=True, observe_trace=True, operation_timeout=1800.0)
    payload = sampling_input(json.loads(cases_path.read_text()))
    for filename, data in (("config.json", config), ("request.json", payload)):
        (root / filename).write_text(json.dumps(data, indent=2) + "\n")
    manifest = dict(
        variant=variant,
        source_commit=source_commit,
        harness_commit=harness,
        phase1_archive_sha256=archive_hash,
        source_config=str(config_path),
        source_cases=str(cases_path),
        request_sha256=hashlib.sha256((root / "request.json").read_bytes()).hexdigest(),
        production_tree_note="historical snapshots; later fixes differ too, not a kernel-only causal comparison",
        requests=1,
        prompt_tokens=4096,
        output_tokens=32,
        trace="includes capture/warmup; analyze only real request markers",
        hardware="not-run",
    )
    (root / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant", choices=("baseline", "phase1", "phase2"), required=True
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(prepare(args.variant, args.root, args.config, args.cases), indent=2)
    )


if __name__ == "__main__":
    main()
