"""Run ticket09's four geometries through the existing full paired acceptance."""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import msgspec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ascend_mempool_pd.mempool_router_protocol import RouterConfig

MATRIX = ((128, 128), (128, 512), (64, 128), (64, 512))


def configurations(base: RouterConfig, sdk: str) -> list[tuple[str, RouterConfig]]:
    return [
        (
            f"b{b}-sd{sd}",
            msgspec.structs.replace(
                base,
                page_size=b,
                decode_capacity=sd,
                prefix_pages=5120 // b,
                context_length=max(base.context_length, 8448 if sd == 128 else 8832),
                max_total_tokens=max(base.max_total_tokens, 8832),
                prefill_shared=True,
                decode_shared=True,
                prefill_index_shared=True,
                prefix_shared=True,
                match_prefix=True,
                verify_prefix=True,
                prefix_read_assignment="by_segment",
                pool_copy=True,
                pool_copy_sdk_build_id=sdk,
            ),
        )
        for b, sd in MATRIX
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("prefill", "decode"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sdk-build-id", required=True)
    parser.add_argument(
        "--page-allocation",
        action="store_true",
        help="ticket18: scattered-page Graph gate and 4K/8K/1K service sequence",
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument(
        "--component-reports", type=Path, nargs=2, metavar=("COPY", "FETCH")
    )
    parser.add_argument("--resume-after-target", type=Path)
    parser.add_argument(
        "--start-from",
        choices=[f"b{b}-sd{sd}" for b, sd in MATRIX],
        help="Resume at this combination; earlier results remain in the previous run",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", args.run_id):
        parser.error(
            "run-id must be a simple new directory name, at most 60 characters"
        )
    configs = configurations(RouterConfig.read(str(args.config)), args.sdk_build_id)
    if args.page_allocation:
        configs = [
            (name, msgspec.structs.replace(c, observe_trace=False))
            for name, c in configs
        ]
    if args.start_from:
        configs = configs[[name for name, _ in configs].index(args.start_from) :]
    if args.resume_after_target and len(configs) != 1:
        parser.error("resume-after-target requires a single geometry")
    if args.describe:
        print(
            json.dumps({name: msgspec.to_builtins(c) for name, c in configs}, indent=2)
        )
        return 0
    root = Path("/home/cryang/log/mempool-min") / args.run_id
    report_path = root / f"matrix-{args.role}.json"
    if report_path.exists():
        parser.error("run-id already has a report; use a new run-id")
    root.mkdir(parents=True, exist_ok=True)
    report = dict(
        status="failed",
        user_accepted=False,
        page_allocation=args.page_allocation,
        combinations={name: dict(status="not-run") for name, _ in configs},
    )
    child = None

    def stop(signum, frame):
        raise KeyboardInterrupt(f"matrix stopped by signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for name, config in configs:
            child_id = f"{args.run_id}-{name}"
            generated = root / f"{name}-{args.role}.json"
            generated.write_bytes(msgspec.json.encode(config))
            report_path.write_text(json.dumps(report, indent=2))
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("run_mempool_min.py")),
                    "--role",
                    args.role,
                    "--config",
                    str(generated),
                    "--mode",
                    "full-acceptance",
                    "--run-id",
                    child_id,
                    "--log-root",
                    str(root),
                    *(["--page-allocation"] if args.page_allocation else []),
                    *(
                        ["--component-reports", *map(str, args.component_reports)]
                        if args.component_reports
                        else []
                    ),
                    *(
                        ["--resume-after-target", str(args.resume_after_target)]
                        if args.resume_after_target
                        else []
                    ),
                ]
            )
            code = child.wait()
            child = None
            entry = report["combinations"][name]
            entry["exit_code"] = code
            entry["report"] = str(root / child_id / args.role / "v1-v10.json")
            entry["status"] = "failed"
            if code:
                return 1
            evidence = json.loads(Path(entry["report"]).read_text())
            if evidence.get("status") != "passed" or not evidence.get("k1"):
                raise ValueError(f"{name}: full K1 evidence missing or failed")
            entry["status"] = "passed"
        report["status"] = "passed"
        report["coverage"] = (
            "remaining combinations only" if args.start_from else "complete matrix"
        )
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc)
        return 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        report_path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    sys.exit(main())
