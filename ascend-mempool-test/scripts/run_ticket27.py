"""One command per host: installed library, LRU components, then off/on service."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from prepare_migration_sample import sampling_input

SCRIPTS = Path(__file__).resolve().parent
LOG_ROOT = Path("/home/cryang/log/k2")


def compare_tokens(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
    left, right = off["output_ids"], on["output_ids"]
    if len(left) != 32 or left != right:
        raise ValueError(
            "LRU off/on complete output token IDs differ or are incomplete"
        )
    return dict(tokens=len(left), equal=True, output_ids=right)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        required=True,
        help="saved cases.json; first case has 4096 input IDs",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--kernel-commit", required=True)
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument(
        "--components-only",
        action="store_true",
        help="smoke only; no service or token comparison",
    )
    args = parser.parse_args()
    if Path(args.run_id).name != args.run_id or args.run_id in ("", ".", ".."):
        parser.error("run-id must be a new directory name")
    root = LOG_ROOT / args.run_id / args.role
    root.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = dict(
        status="failed",
        user_accepted=False,
        coverage="component smoke"
        if args.components_only
        else "ticket27 components and one off/on service pair",
    )
    report["source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SCRIPTS.parents[1], text=True
    ).strip()
    child: subprocess.Popen[Any] | None = None
    component_env = dict(os.environ)
    component_env["PYTHONPATH"] = (
        str(SCRIPTS.parents[1] / "python")
        + os.pathsep
        + component_env.get("PYTHONPATH", "")
    )

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt(f"ticket27 interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def run(
        script: str, arguments: list[str], env: dict[str, str] | None = None
    ) -> None:
        nonlocal child
        with (root / f"{script}-{len(list(root.glob('*.log')))}.log").open("w") as log:
            child = subprocess.Popen(
                [sys.executable, str(SCRIPTS / script), *arguments],
                env=component_env if env is None else env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            code = child.wait()
            child = None
        if code:
            raise RuntimeError(f"{script} returned {code}; see {root}")

    try:
        run(
            "verify_kernel_library.py",
            [
                "--report",
                str(root / "kernel.json"),
                "--kernel-commit",
                args.kernel_commit,
            ],
        )
        run(
            "verify_lru_cache.py",
            ["--report", str(root / "lru.json"), "--device-id", str(args.device_id)],
        )
        if not args.components_only:
            config = json.loads(args.config.read_text())
            config.update(observe=False, observe_trace=False, verify_prefix=False)
            config_path, request_path = root / "config.json", root / "request.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            request_path.write_text(
                json.dumps(sampling_input(json.loads(args.cases.read_text())), indent=2)
                + "\n"
            )
            responses = []
            for enabled in (0, 1):
                run_id = f"{args.run_id}-lru{enabled}"
                env = dict(
                    os.environ,
                    SGLANG_NPU_SPARSE_KV_ENABLE_LRU=str(enabled),
                    SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR="2",
                    SGLANG_NPU_SPARSE_KV_PROBATION_AGE="4",
                )
                run(
                    "diagnose_target_startup.py",
                    [
                        "--role",
                        args.role,
                        "--config",
                        str(config_path),
                        "--run-id",
                        run_id,
                        "--without-index-sync",
                        "--sample-json",
                        str(request_path),
                    ],
                    env,
                )
                stage = LOG_ROOT / run_id / args.role
                summary = json.loads((stage / "summary.json").read_text())
                if summary.get("cleanup") != "passed":
                    raise RuntimeError(f"service cleanup incomplete: {stage}")
                responses.append(
                    json.loads((stage / "sample-response.json").read_text())
                )
            report["comparison"] = compare_tokens(*responses)
        report["status"] = "passed_execution_scope"
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = repr(error)
    finally:
        if child is not None and child.poll() is None:
            # Let the existing component/service entry clean up its own jobs.
            child.terminate()
            child.wait()
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed_execution_scope" else 1


if __name__ == "__main__":
    raise SystemExit(main())
