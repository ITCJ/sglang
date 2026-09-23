"""Remove transfer outputs from before the persistent-worker sweep revision."""

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
NEW_LAYOUT_COMMIT = "5a36b46cf"
OLD_SWEEP_CASE = re.compile(r"bs\d+_n\d+")
OLD_SINGLE_RUN = re.compile(r"transfer_\d{8}T\d{6}_bs\d+_ctx\d+_n\d+")
OLD_PREFLIGHT = re.compile(r"preflight_bs1_\d{8}T\d{6}")
TRANSFER_LOG = re.compile(r"transfer_bs\d+_n\d+_\d{8}T\d{6}_\d+\.log")
COMMIT = re.compile(r"commit: `([0-9a-f]{7,40})`")


def is_old_commit(commit):
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return False
    old_tip = f"{NEW_LAYOUT_COMMIT}^"
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, old_tip],
        cwd=SOURCE_DIR, capture_output=True, check=False,
    ).returncode == 0


def recorded_commit(run_dir):
    report = run_dir / "summary.md"
    if report.is_file():
        match = COMMIT.search(report.read_text(errors="replace")[:1000])
        if match:
            return match.group(1)
    rank_files = sorted(run_dir.rglob("rank_00.json"))
    for rank_file in rank_files:
        try:
            commit = json.loads(rank_file.read_text()).get("git_commit", "")
        except (OSError, ValueError):
            continue
        if commit:
            return commit
    return None


def old_results(results_dir):
    selected = []
    skipped = []
    if not results_dir.is_dir():
        return selected, skipped
    for run_dir in sorted(results_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        if run_dir.name.startswith("transfer_sweep_"):
            old_layout = any(child.is_dir() and OLD_SWEEP_CASE.fullmatch(child.name)
                             for child in run_dir.iterdir())
            new_layout = any((run_dir / f"n{nproc}").exists() for nproc in (1, 16))
            if not old_layout or new_layout:
                continue
        elif not (OLD_SINGLE_RUN.fullmatch(run_dir.name)
                  or OLD_PREFLIGHT.fullmatch(run_dir.name)):
            continue
        commit = recorded_commit(run_dir)
        if commit and is_old_commit(commit):
            selected.append(run_dir)
        else:
            skipped.append(run_dir)
    return selected, skipped


def associated_logs(run_dirs, logs_dir):
    files = set()
    directories = set()
    if not logs_dir.is_dir():
        return files, directories
    logs_root = logs_dir.resolve()
    for run_dir in run_dirs:
        case_dirs = [run_dir]
        if run_dir.name.startswith("transfer_sweep_"):
            case_dirs.extend(child for child in run_dir.iterdir()
                             if child.is_dir() and OLD_SWEEP_CASE.fullmatch(child.name))
        for case_dir in case_dirs:
            pointer = case_dir / "log_path.txt"
            if pointer.is_file():
                log = Path(pointer.read_text().strip()).resolve()
                if (log.parent == logs_root and TRANSFER_LOG.fullmatch(log.name)
                        and log.is_file() and not log.is_symlink()):
                    files.add(log)
            prefix = f"torchrun_{case_dir.name}_"
            for candidate in logs_dir.glob(prefix + "*"):
                if (candidate.is_dir() and not candidate.is_symlink()
                        and re.fullmatch(re.escape(prefix) + r"\d+", candidate.name)):
                    directories.add(candidate)
    return files, directories


def ensure_no_transfer_is_running():
    current_pid = os.getpid()
    result = subprocess.run(
        ["pgrep", "-af", r"transfer_bench\.py|transfer_sweep\.py|run_transfer_bench\.sh"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Cannot check active transfer processes")
    active = [line for line in result.stdout.splitlines()
              if line.split(maxsplit=1)[0] != str(current_pid)]
    if active:
        raise RuntimeError("Transfer benchmark is still running; stop cleanup and wait for it to finish")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path,
                        default=Path(os.environ.get("RESULTS_DIR", SOURCE_DIR / "results")))
    parser.add_argument("--logs-dir", type=Path,
                        default=Path(os.environ.get("LOGS_DIR", SOURCE_DIR / "logs")))
    parser.add_argument("--delete", action="store_true", help="delete listed old outputs; default is preview")
    args = parser.parse_args()
    results_dir = args.results_dir.resolve()
    logs_dir = args.logs_dir.resolve()
    run_dirs, skipped = old_results(results_dir)
    log_files, log_dirs = associated_logs(run_dirs, logs_dir)
    targets = sorted([*run_dirs, *log_files, *log_dirs], key=str)
    for path in targets:
        print(f"{'DELETE' if args.delete else 'WOULD_DELETE'} {path}")
    for path in skipped:
        print(f"SKIP_NO_OLD_COMMIT {path}")
    print(f"Transfer result dirs: {len(run_dirs)}, log files: {len(log_files)}, "
          f"torchrun log dirs: {len(log_dirs)}")
    if args.delete:
        ensure_no_transfer_is_running()
        for path in sorted(log_files):
            path.unlink()
        for path in sorted(log_dirs):
            shutil.rmtree(path)
        for path in sorted(run_dirs):
            shutil.rmtree(path)
        print("OLD_TRANSFER_CLEANUP_OK")


if __name__ == "__main__":
    main()
