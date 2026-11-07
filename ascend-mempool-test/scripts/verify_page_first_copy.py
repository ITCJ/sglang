"""Check page-first indexed copies and graph replay in an NPU-only test arena."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ascend_mempool.alive_pages import PageLease
from ascend_mempool.indexed_copy import copy_rows
from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager, MempoolKVView
from ascend_mempool.offload import MempoolKVOffload


def check_geometry(layout, torch):
    storage = torch.empty(layout.total_bytes // 2, dtype=torch.bfloat16, device="npu")
    base = storage.data_ptr()
    owner = SimpleNamespace(
        rank=0,
        session=None,
        layout=PoolLayout(layout, layout),
        page_tables={},
        page_leases={},
        active_page_versions={},
        view_bases=lambda rank, participant: (base, base),
    )
    owner.view = MempoolKVManager.view.__get__(owner)
    MempoolKVManager.enable_page_tables(owner, "npu")
    pointer = owner.page_tables[0, "alive_main"].data_ptr()
    view = MempoolKVView(owner, 0, layout.layers - 1, layout)
    slots = torch.zeros(3, dtype=torch.int64, device="npu")
    positions = torch.tensor(
        [layout.page_size - 1, layout.page_size, layout.tokens - 1],
        dtype=torch.int64,
        device="npu",
    )
    valid = torch.zeros(3, dtype=torch.bool, device="npu")
    values = (
        torch.arange(3 * layout.dim, device="npu", dtype=torch.int64)
        .remainder(127)
        .to(torch.bfloat16)
        .reshape(3, 1, layout.dim)
    )
    output = torch.full_like(values, -1)
    dtype = torch.empty(1, dtype=torch.bfloat16)
    destination = torch.arange(3, dtype=torch.int64, device="npu")
    writer = MempoolKVOffload(view)
    kernel = torch.ops.npu.unidex_copy
    stream = torch.npu.Stream()
    stream.wait_stream(torch.npu.current_stream())
    with torch.npu.stream(stream):

        def launch():
            writer.write(values, slots=slots, positions=positions, valid=valid)
            copy_rows(
                kernel,
                dtype,
                output,
                slots * view.token_capacity + positions,
                destination,
                valid,
                row_bytes=layout.row_bytes,
                source_view=view,
            )

        launch()
        stream.synchronize()
        graph = torch.npu.NPUGraph()
        with torch.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
            launch()
        for version, enabled in [(1, True), (2, True), (3, False)]:
            slot = 0
            pages = tuple(
                layout.physical_pages - 1 - 2 * i if version == 1 else 2 * i
                for i in range(layout.tokens // layout.page_size)
            )
            MempoolKVManager.install_pages(
                owner, 0, slot, PageLease("probe", version, pages)
            )
            if owner.page_tables[0, "alive_main"].data_ptr() != pointer:
                raise AssertionError("page table address changed")
            slots.fill_(slot)
            valid.fill_(enabled)
            output.fill_(-1)
            graph.replay()
            stream.synchronize()
            expected = values if enabled else torch.full_like(values, -1)
            if not torch.equal(output.cpu(), expected.cpu()):
                raise AssertionError(
                    f"page-first replay content mismatch: slot={slot}, valid={enabled}"
                )
            if enabled:
                # Independent physical tensor view validates the write even if
                # the write and read adapters share an incorrect row formula.
                physical = storage.view(
                    layout.physical_pages,
                    layout.layers,
                    layout.page_size,
                    1,
                    layout.dim,
                )
                oracle = torch.stack(
                    [
                        physical[pages[page], -1, token]
                        for page, token in [
                            (0, layout.page_size - 1),
                            (1, 0),
                            (
                                layout.tokens // layout.page_size - 1,
                                layout.page_size - 1,
                            ),
                        ]
                    ]
                )
                if not torch.equal(oracle.cpu(), values.cpu()):
                    raise AssertionError("physical tensor oracle disagrees with copy")
    torch.npu.synchronize()
    return dict(
        status="passed",
        region_bytes=layout.total_bytes,
        slot_bytes=layout.slot_bytes,
        layers=layout.layers,
        page_size=layout.page_size,
        device_base=hex(base),
        cases=[
            "scattered-pages",
            "64-bit-offset",
            "stable-table-rebind",
            "zero-valid-replay",
        ],
        storage="NPU device arena; no BM allocation or BM readback",
    )


def geometries(page_size):
    return [
        KVLayout(3, 2, 256, 1, 16, page_size=page_size),
        KVLayout(78, 8, 8192, 1, 576, page_size=page_size),
        KVLayout(21, 16, 8192, 1, 128, page_size=page_size),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-size", type=int, choices=(64, 128), default=128)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = dict(status="failed", user_accepted=False, cases=[])
    try:
        import sgl_kernel_npu.sparsity_driven_kv_offload  # noqa: F401
        import torch
        import torch_npu  # noqa: F401

        torch.npu.set_device(0)
        for geometry in geometries(args.page_size):
            result["cases"].append(check_geometry(geometry, torch))
            torch.npu.empty_cache()
        result["status"] = "passed"
    except Exception:
        result["error"] = traceback.format_exc()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
