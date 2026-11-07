"""Check that Docker has both multibase and probation LRU exports installed."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--kernel-commit",
        required=True,
        help="commit used to build the installed wheel",
    )
    args = parser.parse_args()
    report: dict[str, Any] = dict(
        status="failed", supplied_build_commit=args.kernel_commit
    )
    try:
        importlib.import_module("torch_npu")

        from ascend_sparse.kernel_library import kernel_identity

        report.update(kernel_identity())
        report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
