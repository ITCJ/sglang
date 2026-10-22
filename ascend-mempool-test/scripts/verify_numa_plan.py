"""Run the CPU NUMA-plan gate or preview a deployment without opening BM."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT / "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument(
        "--topology", type=Path, help="JSON array of host/ranks/online_nodes records"
    )
    parser.add_argument("--mempool-rank-numa-map")
    parser.add_argument("--role", choices=("prefill", "decode"), default="prefill")
    parser.add_argument("--tp-size", type=int, default=16)
    parser.add_argument("--instance-id", default="plan-preview")
    parser.add_argument("--kv-identity", default="glm52-bf16-example")
    parser.add_argument("--layers", type=int, default=78)
    parser.add_argument("--prefill-capacity", type=int, default=4096)
    parser.add_argument("--decode-capacity", type=int, default=4096)
    parser.add_argument("--kv-lora-rank", type=int, default=512)
    parser.add_argument("--qk-rope-head-dim", type=int, default=64)
    parser.add_argument("--base-port", type=int, default=19000)
    parser.add_argument("--nic-port", type=int, default=25670)
    parser.add_argument("--pool-id", type=int, default=104)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/home/cryang/log/numa-plan") / f"{time.time_ns()}-{os.getpid()}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # Never overwrite an earlier run's evidence.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "status": "running",
        "coverage": "CPU planning only",
        "npu_tested": False,
    }
    try:
        from ascend_mempool.config import MempoolConfig
        from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig

        if args.self_test:
            print(
                f"[1/2] CPU configuration/group/legacy-startup tests: {args.output_dir / 'tests.log'}",
                flush=True,
            )
            suite = unittest.TestSuite()
            for pattern in (
                "test_numa_plan.py",
                "test_config.py",
                "test_pair_startup.py",
                "test_bm_startup_gate.py",
                "test_bm_shared_startup.py",
                "test_sharing_pool.py",
                "test_sharing_shutdown.py",
            ):
                suite.addTests(
                    unittest.defaultTestLoader.discover(
                        str(TEST_ROOT / "tests/unit"), pattern=pattern
                    )
                )
            environment = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("SGLANG_NPU_MEMPOOL_")
            }
            with (
                (args.output_dir / "tests.log").open("w") as log,
                patch.dict(os.environ, environment, clear=True),
                redirect_stderr(log),
                redirect_stdout(log),
            ):
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
            report["tests"] = {
                "run": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            }
            if not result.wasSuccessful():
                raise RuntimeError("CPU NUMA-plan gate failed; see tests.log")
            print("[2/2] Save representative 1/2/4-member plans", flush=True)
            examples = {}
            layout = MempoolConfig(
                prefill_capacity=8, decode_capacity=4
            ).make_mla_layout(num_layers=2, kv_lora_rank=512, qk_rope_head_dim=64)
            for size in (1, 2, 4):
                for share in ("0", "1"):
                    config = MempoolNumaConfig.from_environment(
                        environ={
                            "SGLANG_NPU_MEMPOOL_SHARE": share,
                            "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "0",
                        }
                    )
                    plan = config.plan(
                        hosts=(HostTopology("example-host", tuple(range(size)), "0"),),
                        tp_size=size,
                        role="prefill",
                        instance_id="cpu-gate",
                        kv_identity="fixture",
                        layout=layout,
                    )
                    examples[f"members-{size}-share-{share}"] = plan.report()
            report["examples"] = examples
        else:
            print("[1/2] Validate topology and NUMA assignment", flush=True)
            records = json.loads(args.topology.read_text())
            if not isinstance(records, list):
                raise ValueError(
                    "topology must be an array of host/ranks/online_nodes records"
                )
            hosts = tuple(
                HostTopology(r["host"], tuple(r["ranks"]), r["online_nodes"])
                for r in records
            )
            config = MempoolNumaConfig.from_environment(
                rank_numa_map=args.mempool_rank_numa_map
            )
            layout = MempoolConfig(
                prefill_capacity=args.prefill_capacity,
                decode_capacity=args.decode_capacity,
            ).make_mla_layout(
                num_layers=args.layers,
                kv_lora_rank=args.kv_lora_rank,
                qk_rope_head_dim=args.qk_rope_head_dim,
            )
            plan = config.plan(
                hosts=hosts,
                tp_size=args.tp_size,
                role=args.role,
                instance_id=args.instance_id,
                kv_identity=args.kv_identity,
                layout=layout,
                base_port=args.base_port,
                nic_port=args.nic_port,
                pool_id=args.pool_id,
            )
            report["plan"] = plan.report()
            print("[2/2] Planned rank mapping (no pool allocated)", flush=True)
            for row in report["plan"]["ranks"]:
                print(json.dumps(row, sort_keys=True), flush=True)
            for warning in plan.warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        target = args.output_dir / "report.json"
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"Report: {target}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
