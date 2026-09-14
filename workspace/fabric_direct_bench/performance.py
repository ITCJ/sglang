"""Measured remote Host -> final L1, using the baseline's batching and statistics."""
import csv
import json
import math
import statistics

from check import (LAYERS, PAGE_SIZE, PAGE_BYTES, K_DIM, ROPE_DIM,
                   transfer_plan, check_rc, check_page, print_result)
from kv_transfer_bench import make_batches, measure_batch


def save(cases, directory):
    (directory / "summary.json").write_text(json.dumps({"cases": cases}, indent=2) + "\n")
    with (directory / "summary.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(("tokens", "layout", "path", "median_ms", "p95_ms", "effective_gbps"))
        for case in cases:
            if not case["smoke"]:
                writer.writerow((case["tokens"], case["layout"], "D",
                                 case["median_s"] * 1000, case["p95_s"] * 1000,
                                 case["effective_gbps"]))


def run_client(handle, bm, torch, source_gva, args):
    cases = []
    save(cases, args.run_dir)
    matrix = [(128, "contiguous")] + [(n, layout) for n in
              (1024, 4096, 16384, 65536, 131072) for layout in ("contiguous", "scattered")]
    for tokens, layout in matrix:
        count = tokens // PAGE_SIZE
        smoke = tokens == 128
        print_result(f"R{len(cases)}")
        k = torch.zeros((LAYERS, count + 1, PAGE_SIZE, 1, K_DIM), dtype=torch.bfloat16, device="npu")
        rope = torch.zeros((LAYERS, count + 1, PAGE_SIZE, 1, ROPE_DIM), dtype=torch.bfloat16, device="npu")
        batches = make_batches(count, 8, layout)
        batch_samples = []
        for batch in batches:
            sources, targets, sizes = [], [], []
            for page, slot in zip(batch["pages"], batch["slots"]):
                src, dst, lengths = transfer_plan(source_gva + page * PAGE_BYTES,
                                                 k.data_ptr(), rope.data_ptr(), count, slot)
                sources.extend(src)
                targets.extend(dst)
                sizes.extend(lengths)

            def action():
                check_rc(handle.copy_data_batch(sources, targets, sizes, len(sizes), bm.BmCopyType.GH2L, 0), "GH2L batch")
                check_rc(handle.wait(), "BM wait")

            def prepare():
                for slot in batch["slots"]:
                    k[:, slot].zero_()
                    rope[:, slot].zero_()

            batch_samples.append(measure_batch(action, prepare, torch.npu.synchronize,
                                                0 if smoke else args.warmup,
                                                1 if smoke else args.repeats))
            for page, slot in zip(batch["pages"], batch["slots"]):
                check_page(k, rope, slot, page, torch)
        if torch.count_nonzero(k[:, 0]).item() or torch.count_nonzero(rope[:, 0]).item():
            raise RuntimeError("reserved page overwritten")
        samples = [sum(values) for values in zip(*batch_samples)]
        median = statistics.median(samples)
        case = dict(tokens=tokens, layout=layout, smoke=smoke, code="D", correct=True,
                    bytes=count * PAGE_BYTES, median_s=median,
                    p95_s=sorted(samples)[math.ceil(len(samples) * .95) - 1],
                    effective_gbps=count * PAGE_BYTES / median / 1e9,
                    samples_s=samples, batch_samples_s=batch_samples,
                    warmup=0 if smoke else args.warmup, repeats=len(samples),
                    l1_slots=[slot for batch in batches for slot in batch["slots"]])
        cases.append(case)
        (args.run_dir / f"{tokens}-{layout}.json").write_text(json.dumps(case, indent=2) + "\n")
        save(cases, args.run_dir)
        print_result(f"T{tokens}{'C' if layout == 'contiguous' else 'S'} D={median * 1000:.3f}")
        del k, rope
        torch.npu.empty_cache()
