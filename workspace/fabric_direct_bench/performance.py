"""Whole-request MF transfers: no fixed page batching or summed batch samples."""
import csv
import json
import math
import statistics
import time

from check import (LAYERS, PAGE_SIZE, PAGE_BYTES, K_DIM, ROPE_DIM,
                   transfer_plan, check_rc, check_page, print_result)
from kv_transfer_bench import make_batches, measure_batch, physical_page_slots
from path_names import PATH_NAMES
from unidex_bm import (BmUnidexPlan, PATHS as UNIDEX_PATHS, ROW_TOKENS,
                       MAX_ROW_BYTES)

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


def run_path(code, handle, bm, plans, unidex=None):
    if code in UNIDEX_PATHS:
        unidex.submit(code)
        return
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


def save_row_sweep(cases, directory):
    (directory / "summary.json").write_text(json.dumps({
        "measurement_protocol": "whole_request_v2", "experiment": "unidex_row_sweep",
        "cases": cases}, indent=2) + "\n")
    with (directory / "summary.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(("tokens", "layout", "path", "row_tokens", "k_row_bytes",
                         "rope_row_bytes", "status", "reason", "median_ms", "p95_ms",
                         "effective_gbps", "validation_enabled", "correct", "launch_count"))
        for case in cases:
            writer.writerow((case["tokens"], case["layout"], case["path"],
                             case["row_tokens"], case["k_row_bytes"],
                             case["rope_row_bytes"], case["status"], case.get("reason", ""),
                             case.get("median_s", 0) * 1000 if case["status"] == "ok" else "",
                             case.get("p95_s", 0) * 1000 if case["status"] == "ok" else "",
                             case.get("effective_gbps", ""), case["validation_enabled"],
                             case["correct"], case.get("launch_count", "")))


def run_row_sweep(handle, bm, torch, source_gva, args, runtime_info, owner_registry):
    cases = []
    save_row_sweep(cases, args.run_dir)
    l2_gva = handle.peer_rank_ptr(1, bm.BmMemType.HOST)
    if not l2_gva or l2_gva == source_gva:
        raise RuntimeError("local L2 must belong to client rank 1")
    sizes = ((128, "contiguous"), (4096, "scattered")) if args.unidex_check_only else (
        (131072, "contiguous"), (131072, "scattered"))
    for tokens, layout in sizes:
        count = tokens // PAGE_SIZE
        physical_slots = physical_page_slots(count, layout)
        request = make_batches(count, layout=layout)[0]
        for row_tokens in ROW_TOKENS:
            k_row_bytes = row_tokens * K_DIM * 2
            rope_row_bytes = row_tokens * ROPE_DIM * 2
            if k_row_bytes > MAX_ROW_BYTES:
                reason = f"K row {k_row_bytes} B exceeds official UNIDEX 32768 B limit"
                for code in ("H", "I"):
                    cases.append(dict(tokens=tokens, layout=layout, path=UNIDEX_PATHS[code],
                                      row_tokens=row_tokens, k_row_bytes=k_row_bytes,
                                      rope_row_bytes=rope_row_bytes, status="unsupported",
                                      reason=reason, validation_enabled=False, correct=None))
                save_row_sweep(cases, args.run_dir)
                continue
            owner_start = len(owner_registry)
            k = torch.zeros((LAYERS, physical_slots, PAGE_SIZE, 1, K_DIM),
                            dtype=torch.bfloat16, device="npu")
            rope = torch.zeros((LAYERS, physical_slots, PAGE_SIZE, 1, ROPE_DIM),
                               dtype=torch.bfloat16, device="npu")
            owner_registry.extend((k, rope))
            start = time.perf_counter()
            plans = batch_plans(request, source_gva, l2_gva, k.data_ptr(),
                                rope.data_ptr(), physical_slots)
            unidex = BmUnidexPlan(handle, bm, torch, source_gva, l2_gva, request,
                                   physical_slots, k, rope, args.block_dim, args.timeout,
                                   owner_registry, row_tokens=row_tokens)
            plan_prepare_s = time.perf_counter() - start
            for code in ("H", "I"):
                print_result(f"ROW_SWEEP tokens={tokens} layout={layout} row_tokens={row_tokens} "
                             f"path={UNIDEX_PATHS[code]}")

                def prepare():
                    if code == "H":
                        copy(handle, plans["read"], bm.BmCopyType.G2G)
                    k.zero_()
                    rope.zero_()

                def validate():
                    for page, slot in zip(request["pages"], request["slots"]):
                        check_page(k, rope, slot, page, torch)
                    if torch.count_nonzero(k[:, 0]).item() or torch.count_nonzero(rope[:, 0]).item():
                        raise RuntimeError("reserved L1 page overwritten")
                    guards = [slot for slot in range(physical_slots) if slot not in set(request["slots"])]
                    if guards and (torch.count_nonzero(k[:, guards]).item()
                                   or torch.count_nonzero(rope[:, guards]).item()):
                        raise RuntimeError("unused L1 guard page overwritten")

                samples = measure_batch(
                    lambda: unidex.submit(code), prepare, torch.npu.synchronize,
                    0 if args.unidex_check_only else args.warmup,
                    1 if args.unidex_check_only else args.repeats,
                    validate=validate if args.unidex_check_only or args.validate else None)
                median = statistics.median(samples)
                case = dict(tokens=tokens, layout=layout, path=UNIDEX_PATHS[code],
                            row_tokens=row_tokens, k_row_bytes=k_row_bytes,
                            rope_row_bytes=rope_row_bytes, status="ok",
                            validation_enabled=args.unidex_check_only or args.validate,
                            correct=True if args.unidex_check_only or args.validate else None,
                            bytes=count * PAGE_BYTES, median_s=median,
                            p95_s=sorted(samples)[math.ceil(len(samples) * .95) - 1],
                            effective_gbps=count * PAGE_BYTES / median / 1e9,
                            samples_s=samples, warmup=0 if args.unidex_check_only else args.warmup,
                            repeats=len(samples), plan_prepare_s=plan_prepare_s,
                            measurement_protocol="whole_request_v2", copy_engine="unidex",
                            host_memory="bm_local_mapped" if code == "H" else "bm_remote_mapped",
                            versions=(runtime_info or {}).get("versions", {}),
                            import_sources=(runtime_info or {}).get("import_sources", {}),
                            repo_commit=(runtime_info or {}).get("commit", "unknown"))
                case.update(unidex.metadata)
                case["index_bytes"] = unidex.metadata["index_bytes_by_path"][code]
                case["launch_count"] = unidex.metadata["launch_count_by_path"][code]
                cases.append(case)
                (args.run_dir / f"{tokens}-{layout}-{row_tokens}-{UNIDEX_PATHS[code]}.json").write_text(
                    json.dumps(case, indent=2) + "\n")
                save_row_sweep(cases, args.run_dir)
            torch.npu.synchronize()
            del owner_registry[owner_start:]
            del unidex, k, rope
            torch.npu.empty_cache()
    return cases


def run_client(handle, bm, torch, source_gva, args, runtime_info=None, owner_registry=None):
    if args.include_unidex and owner_registry is None:
        raise RuntimeError("UNIDEX requires worker-owned lifetime registry")
    if args.row_sweep:
        return run_row_sweep(handle, bm, torch, source_gva, args, runtime_info, owner_registry)
    cases = []
    save(cases, args.run_dir)
    codes = (("H", "I") if args.unidex_only else
             FABRIC_CODES + (("H", "I") if args.include_unidex else ()))
    names = {**PATH_NAMES, **UNIDEX_PATHS}
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
        if args.unidex_only:
            matrix.insert(0, (128, "contiguous", True))
    if args.unidex_check_only:
        matrix = [(128, "contiguous", True), (4096, "scattered", True)]
    for tokens, layout, preflight in matrix:
        owner_start = len(owner_registry) if args.include_unidex else 0
        count = tokens // PAGE_SIZE
        physical_slots = physical_page_slots(count, layout)
        smoke = tokens == 128 and not preflight
        case_validate = True if preflight else args.validate
        print_result(f"Running kind={'preflight' if preflight else 'performance'} tokens={tokens} layout={layout}")
        k = torch.zeros((LAYERS, physical_slots, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        if args.include_unidex:
            owner_registry.append(k)
        rope = torch.zeros((LAYERS, physical_slots, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        if args.include_unidex:
            owner_registry.append(rope)
        request = make_batches(count, layout=layout)[0]
        request_samples = {}
        plan_start = time.perf_counter()
        plans = batch_plans(request, source_gva, l2_gva, k.data_ptr(), rope.data_ptr(), physical_slots)
        unidex = (BmUnidexPlan(handle, bm, torch, source_gva, l2_gva, request,
                               physical_slots, k, rope, args.block_dim, args.timeout,
                               owner_registry)
                  if args.include_unidex else None)
        plan_prepare_s = time.perf_counter() - plan_start
        for code in codes:
            print(f"MEASURE path={names[code]} tokens={tokens} layout={layout} whole_request", flush=True)
            def prepare():
                if code in ("E", "H"):
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
                lambda: run_path(code, handle, bm, plans, unidex), prepare, torch.npu.synchronize,
                0 if smoke or preflight else args.warmup,
                1 if smoke or preflight else args.repeats,
                validate=validate if case_validate else None)
        for code in codes:
            samples = request_samples[code]
            median = statistics.median(samples)
            case = dict(tokens=tokens, layout=layout, smoke=smoke, preflight=preflight,
                    path=names[code], validation_enabled=case_validate,
                    correct=True if case_validate else None, l2_bytes=l2_bytes(physical_slots) if code not in ("D", "I") else 0,
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
            if code in UNIDEX_PATHS:
                case.update(unidex.metadata)
                case.update(status="ok", plan_prepare_s=plan_prepare_s,
                            copy_engine="unidex",
                            host_memory="bm_remote_mapped" if code == "I" else "bm_local_mapped",
                            versions=(runtime_info or {}).get("versions", {}),
                            import_sources=(runtime_info or {}).get("import_sources", {}),
                            repo_commit=(runtime_info or {}).get("commit", "unknown"))
                case["index_bytes"] = unidex.metadata["index_bytes_by_path"][code]
                case["launch_count"] = unidex.metadata["launch_count_by_path"][code]
            cases.append(case)
            prefix = "preflight-" if preflight else ""
            (args.run_dir / f"{prefix}{tokens}-{layout}-{names[code]}.json").write_text(json.dumps(case, indent=2) + "\n")
            save(cases, args.run_dir)
        for case in cases[-len(codes):]:
            print_result(f"tokens={tokens} layout={layout} {case['path']}={case['median_s'] * 1000:.3f} ms")
        if args.include_unidex:
            torch.npu.synchronize()
            del owner_registry[owner_start:]
        del unidex
        del k, rope
        torch.npu.empty_cache()
