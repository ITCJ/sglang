"""Eager swap-in experiment geometry, workload and small JSON summaries."""

from __future__ import annotations

import math
import random
import statistics

BASELINE = "aa5852624e15df139df7c76bb8d757341b049713"
SOURCE_ROWS = dict(
    HBM_cache=1206643520, P_alive=280470816, D_alive=3565280, HBM_current=725312
)
ITERATIONS = 100
TOPK = 2048


def source_counts(total: int) -> dict[str, int]:
    """Largest remainder allocation from unrounded observation counts."""
    denominator = sum(SOURCE_ROWS.values())
    counts = {k: total * v // denominator for k, v in SOURCE_ROWS.items()}
    ordered = sorted(
        SOURCE_ROWS, key=lambda k: total * SOURCE_ROWS[k] % denominator, reverse=True
    )
    for k in ordered[: total - sum(counts.values())]:
        counts[k] += 1
    return counts


def selection(batch: int, iteration: int) -> dict:
    """Same per-request proportions; vary addresses and selected-row order.

    This synthetic selection is prepared outside timing, like an upstream top-k
    result. Both versions rebuild their metadata from these same device inputs.
    """
    counts = source_counts(TOPK)
    rng = random.Random(26029 + iteration)
    positions, hits, slots, sources, indices = [], [], [], [], []
    for request in range(batch):
        prompt = rng.sample(range(8192), counts["HBM_cache"] + counts["P_alive"])
        records = []
        for j, position in enumerate(prompt):
            hit = j < counts["HBM_cache"]
            records.append(
                (
                    position,
                    hit,
                    j if hit else 0,
                    "HBM_cache" if hit else "P_alive",
                    j if hit else position,
                )
            )
        for position in rng.sample(range(511), counts["D_alive"]):
            records.append((8192 + position, False, 0, "D_alive", position))
        for _ in range(counts["HBM_current"]):
            records.append((8703, False, 0, "HBM_current", request + 1))
        rng.shuffle(records)
        positions.append([r[0] for r in records])
        hits.append([r[1] for r in records])
        slots.append([r[2] for r in records])
        sources.append([r[3] for r in records])
        indices.append([r[4] for r in records])
    return dict(
        positions=positions,
        hits=hits,
        slots=slots,
        sources=sources,
        indices=indices,
        counts={k: v * batch for k, v in counts.items()},
    )


def pool_plan(tp: int, batch: int, side: int, host: str, port: int, run_id: str):
    from .layout import KVLayout, PoolLayout
    from .sharing_plan import HostTopology, MempoolNumaConfig

    nodes = tuple(range(8)) if tp == 16 else (0,)
    common = dict(layers=78, slots=batch, heads=1, dim=576, page_size=64)
    layout = PoolLayout(KVLayout(tokens=8192, **common), KVLayout(tokens=512, **common))
    return MempoolNumaConfig(False, ",".join(map(str, nodes))).plan(
        hosts=(HostTopology(host, tuple(range(tp)), ",".join(map(str, nodes))),),
        tp_size=tp,
        role=("prefill", "decode")[side],
        instance_id=run_id,
        kv_identity="ticket26-eager-swapin",
        layout=layout,
        base_port=port + 2,
        nic_port=port + 64,
        pool_id=129,
        prefill_shared=True,
        decode_shared=True,
    )


def stats(values: list[float]) -> dict:
    values = sorted(values)
    return dict(
        n=len(values),
        mean=statistics.mean(values),
        median=statistics.median(values),
        p95=values[math.ceil(0.95 * len(values)) - 1],
        minimum=values[0],
        maximum=values[-1],
    )


def summarize(ranks: list[dict], *, graph=False) -> dict:
    result = {}
    for variant, batch in (
        (v, b) for b in (1, 16) for v in ("baseline", "materialize")
    ):
        per_rank = {}
        for rank in ranks:
            rows = [
                s
                for s in rank["samples"]
                if s["variant"] == variant and s["batch"] == batch
            ]
            if len(rows) != ITERATIONS:
                raise ValueError("expected 100 complete samples per D rank and version")
            names = (
                ("total_ms", "graph_ms")
                if graph
                else (
                    "total_ms",
                    "metadata_host_ms",
                    "metadata_device_span_ms",
                    "kernel_sum_ms",
                    "copy_envelope_ms",
                )
            )
            per_rank[str(rank["rank"])] = {
                name: stats([r[name] for r in rows]) for name in names
            }
            if not graph:
                per_rank[str(rank["rank"])]["kernels_ms"] = {
                    k: stats([r["kernels_ms"][k] for r in rows])
                    for k in rows[0]["kernels_ms"]
                }
        # D ranks share one host monotonic clock. No P/D clock subtraction.
        group = []
        for iteration in range(ITERATIONS):
            rows = [
                next(
                    s
                    for s in r["samples"]
                    if s["variant"] == variant
                    and s["iteration"] == iteration
                    and s["batch"] == batch
                )
                for r in ranks
            ]
            group.append(
                dict(
                    iteration=iteration,
                    elapsed_ms=(
                        max(r["end_ns"] for r in rows)
                        - min(r["start_ns"] for r in rows)
                    )
                    / 1e6,
                    launch_skew_ms=(
                        max(r["start_ns"] for r in rows)
                        - min(r["start_ns"] for r in rows)
                    )
                    / 1e6,
                )
            )
        result[f"{variant}/b{batch}"] = dict(
            per_rank=per_rank,
            group_samples=group,
            group_elapsed_ms=stats([r["elapsed_ms"] for r in group]),
        )
    return result
