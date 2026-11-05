"""Rebuild ticket14 reports from immutable archived samples; no NPU or peer startup."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bandwidth_summary import finish_report
from ascend_mempool.bandwidth_report import save_report


def analyze(
    root: Path, output: Path, confirmation: str, layer_window_ms: float | None
) -> None:
    runner = json.loads((root / "runner.json").read_text())
    if runner.get("status") != "measured" or runner.get("cleanup") != "passed":
        raise ValueError("archive must have measured status and passed cleanup")
    identity = json.loads((root / "identity.json").read_text())
    config = identity["plan"]["benchmark"]
    for prefix in ("", "peer/"):
        for rank in range(config["tp_size"]):
            status = json.loads((root / f"{prefix}rank-{rank}/status.json").read_text())
            if status.get("status") != "passed" or status.get("rank") != rank:
                raise ValueError(f"worker did not pass: {prefix}rank-{rank}")
    rows = [r for p in root.glob("**/samples.json") for r in json.loads(p.read_text())]
    output.mkdir(parents=True, exist_ok=False)
    metadata: dict[str, Any] = dict(
        runner,
        config=config,
        scope=config["suite"],
        sdk_source_confirmation=confirmation,
        layer_window_ms=layer_window_ms,
        analysis_source=str(root.resolve()),
        measurement_identity=identity,
        overlap="not_proven_by_transfer_only_microbenchmark",
        status="analysis_pending",
    )
    try:
        save_report(output, rows, metadata)
        finish_report(root, argparse.Namespace(layer_window_ms=layer_window_ms), output)
    except Exception as error:
        (output / "report.json").write_text(
            json.dumps(
                dict(
                    status="failed",
                    error=str(error),
                    analysis_source=str(root),
                    user_accepted=False,
                ),
                indent=2,
            )
            + "\n"
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--sdk-source-confirmation",
        default="",
        help="User confirmation of identical SDK source HEAD; optionally include the actual SHA",
    )
    parser.add_argument("--layer-window-ms", type=float)
    args = parser.parse_args()
    if args.layer_window_ms is not None and (
        not math.isfinite(args.layer_window_ms) or args.layer_window_ms <= 0
    ):
        parser.error("layer-window-ms must be finite and positive")
    try:
        analyze(
            args.input, args.output, args.sdk_source_confirmation, args.layer_window_ms
        )
    except Exception as error:
        print(f"TICKET14_ANALYSIS_FAILED {error}", file=sys.stderr)
        return 1
    print(f"TICKET14_ANALYSIS_COMPLETE report={args.output / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
