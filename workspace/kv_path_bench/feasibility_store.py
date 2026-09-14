#!/usr/bin/env python3
"""Start one small or maximum-size Store dataset for the feasibility check."""

import argparse
import json
import os
import traceback

from feasibility_log import enable_log
from store_server import main as serve


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store_ip")
    parser.add_argument("size", choices=("small", "max"))
    args = parser.parse_args()
    enable_log("store", args.size)

    try:
        tokens, segment_gib = (128, 1) if args.size == "small" else (131072, 10)
        if args.size == "max":
            os.environ.setdefault(
                "ASCEND_GLOBAL_RESOURCE_CONFIG",
                '{"fabric_memory.max_capacity":16}',
            )
            config = json.loads(os.environ["ASCEND_GLOBAL_RESOURCE_CONFIG"])
            if int(config.get("fabric_memory.max_capacity", 0)) < 16:
                raise RuntimeError("max needs fabric_memory.max_capacity >= 16")

        print(
            f"FEASIBILITY_STORE size={args.size} tokens={tokens} segment_gib={segment_gib}",
            flush=True,
        )
        return serve(
            [
                "--local-ip",
                args.store_ip,
                "--tokens",
                str(tokens),
                "--segment-gib",
                str(segment_gib),
                "--master-log",
                f"/tmp/a3-kv-feasibility-master-{args.size}.log",
            ],
            ready_code="S0" if args.size == "small" else "S1",
        )
    except Exception:
        traceback.print_exc()
        print("F1", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
