"""Probe ticket26's eager counters without loading a model or connecting BM.

Run on one idle device. Synchronize each named call to narrow a device failure
to that call, instead of observing it at a later allocation or event operation.
This diagnosis is not a service or Graph acceptance result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="npu:0")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/home/cryang/log/k2/k2-ticket26-008-index-add/report.json"),
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(
        status="running",
        coverage="eager counters only; no model/BM/Graph",
        device=args.device,
        checks=[],
    )

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    def begin(name):
        report["current"] = name
        save()
        print("BEGIN " + name, flush=True)

    try:
        begin("device_setup")
        import torch

        if args.device.startswith("npu"):
            import torch_npu

            torch.npu.set_device(args.device)
            report["torch_npu"] = torch_npu.__version__
            synchronize = torch.npu.synchronize
        else:
            synchronize = lambda: None
        report.update(
            torch=torch.__version__,
            allocator=os.environ.get("PYTORCH_NPU_ALLOC_CONF"),
            task_queue=os.environ.get("TASK_QUEUE_ENABLE"),
        )
        from ascend_mempool.observation_device import PageObservation

        # Same widths/dtypes as the failing bs16, req_pool_rows17 service.
        observation = PageObservation(78, 17, 8832, 64, 2048, (0,), 0, args.device)
        checks = torch.zeros((78, 17, 3), dtype=torch.int64, device=args.device)
        sources = torch.zeros((78, 17, 2), dtype=torch.int64, device=args.device)
        actual = torch.zeros(
            (16, 2048, 1, 576), dtype=torch.bfloat16, device=args.device
        )
        selected = torch.zeros_like(actual)
        actual[0, 0, 0, 0] = 1
        synchronize()

        def check(name, call, result, expected):
            begin(name)
            call()
            synchronize()
            got = result.cpu()
            want = torch.zeros_like(got)
            if active:
                want[16] = torch.tensor(expected, dtype=got.dtype)
            torch.testing.assert_close(got, want, rtol=0, atol=0)
            report["checks"].append(dict(name=name, status="passed"))
            save()
            print("PASS " + name, flush=True)

        for active, layer in ((False, 0), (True, 77)):
            case = "one_active_padded" if active else "all_padding"
            begin(case + "/inputs")
            rows = torch.zeros(16, dtype=torch.int64, device=args.device)
            valid = torch.zeros((16, 2048), dtype=torch.bool, device=args.device)
            hits = torch.zeros_like(valid)
            previous = torch.zeros(16, dtype=torch.int64, device=args.device)
            updates = torch.zeros((16, 3), dtype=torch.int64, device=args.device)
            io = torch.zeros((16, 2), dtype=torch.int64, device=args.device)
            present = torch.zeros_like(previous)
            if active:
                rows[0] = 16
                valid[0, :4] = True
                hits[0, :2] = True
                previous[0] = 3
                updates[0, 0], updates[0, 2] = 1, -1
                io[0, 0], io[0, 1] = 3, 1
                present[0] = 5
            expected = torch.arange(2048, device=args.device).expand(16, -1)
            positions = expected.clone()
            positions[0, 0] = -1
            found = valid.clone()
            found[0, 1] = False
            synchronize()
            check(
                case + "/runtime_fetch_checks",
                lambda: checks[layer].index_add_(0, rows, updates),
                checks[layer],
                [1, 0, -1],
            )
            check(
                case + "/observation_cache_updates",
                lambda: observation.cache_updates(layer, rows, previous, hits, valid),
                observation.cache_changes[layer],
                [1, 4, 4],
            )
            check(
                case + "/observation_refill_verified",
                lambda: observation.refill_verified(
                    layer, rows, valid, actual, selected
                ),
                observation.refill_check[layer],
                [4, 1],
            )
            check(
                case + "/observation_map_verified",
                lambda: observation.map_verified(
                    layer, rows, valid, found, positions, expected, present
                ),
                observation.map_check[layer],
                [4, 2, 1],
            )
            check(
                case + "/runtime_fetch_sources",
                lambda: sources[layer].index_add_(0, rows, io),
                sources[layer],
                [3, 1],
            )
        report.update(status="passed", current="complete")
        print("INDEX_ADD_PROBE_PASSED (eager diagnosis only)", flush=True)
        return 0
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        traceback.print_exc()
        return 1
    finally:
        save()


if __name__ == "__main__":
    raise SystemExit(main())
