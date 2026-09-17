"""Whole-request MF transfers: no fixed page batching or summed batch samples."""
import csv
import json
import math
import statistics

from check import (LAYERS, PAGE_SIZE, PAGE_BYTES, K_DIM, ROPE_DIM,
                   transfer_plan, check_rc, check_page, print_result)
from kv_transfer_bench import make_batches, measure_batch, physical_page_slots
from path_names import PATH_NAMES

FABRIC_CODES = ("E", "F", "D", "G")
K_PAGE = LAYERS * PAGE_SIZE * K_DIM * 2
ROPE_PAGE = PAGE_BYTES - K_PAGE
MAX_PAGES = 131072 // PAGE_SIZE
SCATTER_SOURCE_BASE = MAX_PAGES * PAGE_BYTES

def l2_bytes(physical_slots):
    return physical_slots * PAGE_BYTES


def batch_plans(batch, source_gva, l2_gva, k_ptr, rope_ptr, physical_slots):
    """Final L2 matches baseline: separate page-first K/RoPE, reserved slot 0."""
    remote, local, targets, sizes = [], [], [], []
    read_src, read_dst, read_size = [], [], []
    for page, slot in zip(batch["pages"], batch["slots"]):
        source_page = (page if batch["layout"] == "contiguous"
                       else MAX_PAGES + 1 + 2 * page)
        src, dst, lengths = transfer_plan(source_gva + source_page * PAGE_BYTES,
                                         k_ptr, rope_ptr, physical_slots - 1, slot)
        remote.extend(src)
        targets.extend(dst)
        sizes.extend(lengths)
        lk = l2_gva + slot * K_PAGE
        lr = l2_gva + physical_slots * K_PAGE + slot * ROPE_PAGE
        for layer in range(LAYERS):
            local.extend((lk + layer * K_PAGE // LAYERS,
                          lr + layer * ROPE_PAGE // LAYERS))
        read_src.extend((source_gva + source_page * PAGE_BYTES,
                         source_gva + source_page * PAGE_BYTES + K_PAGE))
        read_dst.extend((lk, lr))
        read_size.extend((K_PAGE, ROPE_PAGE))
    return {"read": (read_src, read_dst, read_size),
            "local": (local, targets, sizes), "direct": (remote, targets, sizes)}


def copy(handle, plan, kind):
    sources, targets, sizes = plan
    check_rc(handle.copy_data_batch(sources, targets, sizes, len(sizes), kind, 0), "BM batch")
    check_rc(handle.wait(), "BM wait")


def run_path(code, handle, bm, plans):
    if code in ("F", "G"):
        copy(handle, plans["read"], bm.BmCopyType.G2G)
    if code == "G":
        return
    copy(handle, plans["direct" if code == "D" else "local"], bm.BmCopyType.GH2L)


def save(cases, directory):
    (directory / "summary.json").write_text(json.dumps({"measurement_protocol": "whole_request_v2", "cases": cases}, indent=2) + "\n")
    with (directory / "summary.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(("tokens", "layout", "path", "median_ms", "p95_ms", "effective_gbps"))
        for case in cases:
            if not case["smoke"] and not case.get("preflight", False):
                writer.writerow((case["tokens"], case["layout"], case["path"],
                                 case["median_s"] * 1000, case["p95_s"] * 1000,
                                 case["effective_gbps"]))


def run_client(handle, bm, torch, source_gva, args):
    cases = []
    save(cases, args.run_dir)
    l2_gva = handle.peer_rank_ptr(1, bm.BmMemType.HOST)
    if not l2_gva or l2_gva == source_gva:
        raise RuntimeError("local L2 must belong to client rank 1")
    # Reset L2 outside timing so a missing remote read cannot pass on stale E data.
    # Page-size scratch limits CPU clearing memory; clearing is outside timing.
    zero = torch.zeros(PAGE_BYTES, dtype=torch.uint8, device="cpu")
    matrix = ([(args.tokens, args.layout, False)] if args.tokens is not None else
              [(128, "contiguous", False)] + [(n, layout, False) for n in
               (1024, 4096, 16384, 65536, 131072) for layout in ("contiguous", "scattered")])
    if args.preflight_validate:
        matrix.insert(0, (4096, "scattered", True))
    for tokens, layout, preflight in matrix:
        count = tokens // PAGE_SIZE
        physical_slots = physical_page_slots(count, layout)
        smoke = tokens == 128 and not preflight
        case_validate = True if preflight else args.validate
        print_result(f"Running kind={'preflight' if preflight else 'performance'} tokens={tokens} layout={layout}")
        k = torch.zeros((LAYERS, physical_slots, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        rope = torch.zeros((LAYERS, physical_slots, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        request = make_batches(count, layout=layout)[0]
        request_samples = {}
        plans = batch_plans(request, source_gva, l2_gva, k.data_ptr(), rope.data_ptr(), physical_slots)
        for code in FABRIC_CODES:
            print(f"MEASURE path={PATH_NAMES[code]} tokens={tokens} layout={layout} whole_request", flush=True)
            def prepare():
                if code == "E":
                    copy(handle, plans["read"], bm.BmCopyType.G2G)
                elif code in ("F", "G"):
                    # Clear in page-sized pieces; this is not timed transfer batching.
                    for offset in range(0, l2_bytes(physical_slots), PAGE_BYTES):
                        check_rc(handle.copy_data(zero.data_ptr(), l2_gva + offset, PAGE_BYTES,
                                                  bm.BmCopyType.H2GH, 0), "clear L2")
                        check_rc(handle.wait(), "clear L2 wait")
                k.zero_()
                rope.zero_()

            def validate():
                if code == "G":
                    copy(handle, plans["local"], bm.BmCopyType.GH2L)
                    torch.npu.synchronize()
                for page, slot in zip(request["pages"], request["slots"]):
                    check_page(k, rope, slot, page, torch)
                if torch.count_nonzero(k[:, 0]).item() or torch.count_nonzero(rope[:, 0]).item():
                    raise RuntimeError("reserved page overwritten")
                guards = [slot for slot in range(physical_slots) if slot not in set(request["slots"])]
                if (torch.count_nonzero(k[:, guards]).item()
                        or torch.count_nonzero(rope[:, guards]).item()):
                    raise RuntimeError("unused L1 guard page overwritten")

            request_samples[code] = measure_batch(
                lambda: run_path(code, handle, bm, plans), prepare, torch.npu.synchronize,
                0 if smoke or preflight else args.warmup,
                1 if smoke or preflight else args.repeats,
                validate=validate if case_validate else None)
        for code in FABRIC_CODES:
            samples = request_samples[code]
            median = statistics.median(samples)
            case = dict(tokens=tokens, layout=layout, smoke=smoke, preflight=preflight,
                    path=PATH_NAMES[code], validation_enabled=case_validate,
                    correct=True if case_validate else None, l2_bytes=l2_bytes(physical_slots) if code != "D" else 0,
                    allocated_l2_bytes=l2_bytes(physical_slots), physical_page_slots=physical_slots,
                    measurement_protocol="whole_request_v2",
                    request_pages=count, timing="one synchronized whole-request wall time per sample",
                    bytes=count * PAGE_BYTES, median_s=median,
                    p95_s=sorted(samples)[math.ceil(len(samples) * .95) - 1],
                    effective_gbps=count * PAGE_BYTES / median / 1e9,
                    samples_s=samples,
                    warmup=0 if smoke or preflight else args.warmup, repeats=len(samples),
                    l1_slots=request["slots"], l2_slots=request["slots"],
                    remote_source_slots=[(page if layout == "contiguous"
                                          else MAX_PAGES + 1 + 2 * page)
                                         for page in request["pages"]],
                    mapping_version="page_gap_v2")
            cases.append(case)
            prefix = "preflight-" if preflight else ""
            (args.run_dir / f"{prefix}{tokens}-{layout}-{PATH_NAMES[code]}.json").write_text(json.dumps(case, indent=2) + "\n")
            save(cases, args.run_dir)
        for case in cases[-len(FABRIC_CODES):]:
            print_result(f"tokens={tokens} layout={layout} {case['path']}={case['median_s'] * 1000:.3f} ms")
        del k, rope
        torch.npu.empty_cache()
