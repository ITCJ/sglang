"""Measured remote Host -> final L1, using the baseline's batching and statistics."""
import csv
import json
import math
import statistics

from check import (LAYERS, PAGE_SIZE, PAGE_BYTES, K_DIM, ROPE_DIM,
                   transfer_plan, check_rc, check_page, print_result)
from kv_transfer_bench import make_batches, measure_batch
from path_names import PATH_NAMES

FABRIC_CODES = ("E", "F", "D", "G")
K_PAGE = LAYERS * PAGE_SIZE * K_DIM * 2
ROPE_PAGE = PAGE_BYTES - K_PAGE
L2_K_BYTES = 9 * K_PAGE
L2_BYTES = 9 * PAGE_BYTES


def batch_plans(batch, source_gva, l2_gva, k_ptr, rope_ptr, count):
    """Final L2 matches baseline: separate page-first K/RoPE, reserved slot 0."""
    remote, local, targets, sizes = [], [], [], []
    read_src, read_dst, read_size = [], [], []
    for index, (page, slot) in enumerate(zip(batch["pages"], batch["slots"]), 1):
        src, dst, lengths = transfer_plan(source_gva + page * PAGE_BYTES,
                                         k_ptr, rope_ptr, count, slot)
        remote.extend(src)
        targets.extend(dst)
        sizes.extend(lengths)
        lk = l2_gva + index * K_PAGE
        lr = l2_gva + L2_K_BYTES + index * ROPE_PAGE
        for layer in range(LAYERS):
            local.extend((lk + layer * K_PAGE // LAYERS,
                          lr + layer * ROPE_PAGE // LAYERS))
        read_src.extend((source_gva + page * PAGE_BYTES,
                         source_gva + page * PAGE_BYTES + K_PAGE))
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
    (directory / "summary.json").write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    with (directory / "summary.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(("tokens", "layout", "path", "median_ms", "p95_ms", "effective_gbps"))
        for case in cases:
            if not case["smoke"]:
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
    zero = torch.zeros(L2_BYTES, dtype=torch.uint8, device="cpu")
    matrix = [(128, "contiguous")] + [(n, layout) for n in
              (1024, 4096, 16384, 65536, 131072) for layout in ("contiguous", "scattered")]
    for tokens, layout in matrix:
        count = tokens // PAGE_SIZE
        smoke = tokens == 128
        print_result(f"Running tokens={tokens} layout={layout}")
        k = torch.zeros((LAYERS, count + 1, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        rope = torch.zeros((LAYERS, count + 1, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        batches = make_batches(count, 8, layout)
        batch_samples = {code: [] for code in FABRIC_CODES}
        for batch_index, batch in enumerate(batches):
            plans = batch_plans(batch, source_gva, l2_gva, k.data_ptr(), rope.data_ptr(), count)
            offset = batch_index % len(FABRIC_CODES)
            order = FABRIC_CODES[offset:] + FABRIC_CODES[:offset]
            for code in order:
                print(f"MEASURE path={PATH_NAMES[code]} tokens={tokens} layout={layout} batch={batch_index}", flush=True)
                def prepare():
                    if code == "E":
                        # Populate final L2 before timing the local-only path.
                        copy(handle, plans["read"], bm.BmCopyType.G2G)
                    elif code in ("F", "G"):
                        check_rc(handle.copy_data(zero.data_ptr(), l2_gva, L2_BYTES,
                                                  bm.BmCopyType.H2GH, 0), "clear L2")
                        check_rc(handle.wait(), "clear L2 wait")
                    for slot in batch["slots"]:
                        k[:, slot].zero_()
                        rope[:, slot].zero_()

                batch_samples[code].append(measure_batch(
                    lambda: run_path(code, handle, bm, plans), prepare, torch.npu.synchronize,
                    0 if smoke else args.warmup, 1 if smoke else args.repeats))
                if code == "G":
                    # Validate L2 through the existing local copy, strictly OUTSIDE timing.
                    copy(handle, plans["local"], bm.BmCopyType.GH2L)
                    torch.npu.synchronize()
                for page, slot in zip(batch["pages"], batch["slots"]):
                    check_page(k, rope, slot, page, torch)
        if torch.count_nonzero(k[:, 0]).item() or torch.count_nonzero(rope[:, 0]).item():
            raise RuntimeError("reserved page overwritten")
        for code in FABRIC_CODES:
            samples = [sum(values) for values in zip(*batch_samples[code])]
            median = statistics.median(samples)
            case = dict(tokens=tokens, layout=layout, smoke=smoke,
                    path=PATH_NAMES[code], correct=True, l2_bytes=L2_BYTES if code != "D" else 0,
                    bytes=count * PAGE_BYTES, median_s=median,
                    p95_s=sorted(samples)[math.ceil(len(samples) * .95) - 1],
                    effective_gbps=count * PAGE_BYTES / median / 1e9,
                    samples_s=samples, batch_samples_s=batch_samples[code],
                    warmup=0 if smoke else args.warmup, repeats=len(samples),
                    l1_slots=[slot for batch in batches for slot in batch["slots"]])
            cases.append(case)
            (args.run_dir / f"{tokens}-{layout}-{PATH_NAMES[code]}.json").write_text(json.dumps(case, indent=2) + "\n")
            save(cases, args.run_dir)
        for case in cases[-len(FABRIC_CODES):]:
            print_result(f"tokens={tokens} layout={layout} {case['path']}={case['median_s'] * 1000:.3f} ms")
        del k, rope
        torch.npu.empty_cache()
