# Dense Three-Tier KV Experiment 0

This directory makes Experiment 0 in
`DENSE_THREE_TIER_KV_EXPERIMENT_PLAN.md` reproducible against an unmodified
upstream-main snapshot. The experiment branch is based on
`197832bcf536543092e621e03d61ae2602a392d0` (2026-08-12), the latest upstream
main commit merged into `yhc/sparsity-driven-kv-offload`. No sparsity/offload
changes from that branch are present.

## Correctness Notes

Two plan assumptions do not match the selected official implementation or the
specified capacity:

- Ascend plus MLA forces `kernel_ascend + page_first_kv_split` in
  `python/sglang/srt/hardware_backend/npu/utils.py`. The plan says
  `page_first_direct`, but forcing that value would require modifying the
  official path. The launcher and preflight therefore require the effective
  official `page_first_kv_split` layout.
- Ten 64K prompts plus the required filler cannot coexist in a 20 GB/rank L2.
  The runner uses an isolated L2 preparation for each prefix, verifies its
  cache source, waits for write-through, clears only L1/L2, and then moves to
  the next prefix. The external L3 is preserved throughout this sequence.
  After all ten L2 measurements it restarts only the SGLang worker, preserves
  external Mooncake, and measures the same ten prefixes from L3 in the same
  order.

The capacity guard uses startup values rather than silently accepting an
underfilled cache. Expected values for BF16 DeepSeek-V3.1 MLA are:

| Length | Cacheable request tokens | L1 | Filler | Isolated L2 need | Original batch need |
| --- | ---: | ---: | ---: | ---: | ---: |
| 64K | 65,472 | 73,728 | 88,512 | 153,984 (~10.82 GB) | 743,232 (~52.23 GB) |
| 128K | 131,008 | 139,264 | 167,168 | 298,176 (~20.95 GB) | 1,477,248 (~103.81 GB) |

A 20 GB/rank BF16 MLA L2 holds about 284,608 tokens. Thus 64K runs with the
isolated protocol, while the 128K preflight intentionally returns `skipped`.
Running 128K needs at least about 21 GB/rank (22 GB minimum practical setting),
which is a deliberate deviation from the fixed 20 GB plan configuration.

## What Is Included

- `run_exp0.sh`: end-to-end three-run driver.
- `server_ctl.sh`: fixed-parameter SGLang launcher and worker restart control.
- `exp0.py`: deterministic token manifests, cache control, TTFT measurement,
  source admission, and capacity/config preflight.
- `summarize.py`: paired per-prefix and across-run statistics.
- `collect_env.sh`: HCCS/RDMA/NIC/NUMA and software environment capture.
- `mooncake_*.example.json`: external 640 GB Mooncake store and zero-contribution
  worker examples.
- `tests/test_exp0.py`: dependency-free unit tests for the client and summary.

The branch also adds `POST /wait_until_idle?timeout=...`. Unlike
`/flush_cache`, it waits for every DP scheduler and all in-flight HiCache
write-through/load/prefetch/backup operations without clearing L1 or L2.

## Prerequisites

Use the same Python environment for the server and client. It must contain the
Ascend SGLang runtime, `transformers`, and Mooncake. Pin
`EXP0_MODEL_REVISION` to the exact model commit; the driver rejects an unpinned
manifest.

Run Mooncake outside the SGLang worker so its contents survive worker restarts.
One supported arrangement is:

```bash
mooncake_master \
  --enable_http_metadata_server=true \
  --http_metadata_server_port=8080 \
  --eviction_high_watermark_ratio=0.95

python -m mooncake.mooncake_store_service \
  --config=/absolute/path/to/mooncake_store.json \
  --port=8081
```

Keep those two processes alive for the complete run. The store example uses
the integer `640000000000`, so the allocation is 640 decimal GB rather than
the Mooncake parser's GiB interpretation of a `640gb` string. The worker
example contributes zero memory and connects to that external store.

Create local configurations from `config.env.example`,
`mooncake_worker.example.json`, and `mooncake_store.example.json`. Replace all
`CHANGE_ME` values, including reachable hostnames and the exact model revision.
`EXP0_EXTRA_SERVER_ARGS` is only for model/runtime flags such as ModelSlim
quantization; fixed experiment flags cannot be overridden there.

## Run

The primary 64K experiment is:

```bash
workspace/dense_three_tier_kv_exp0/run_exp0.sh 64k
```

It performs three independent runs by default. Each run starts from empty
L1/L2/L3, warms and evicts each prefix, admits only a strict L2 source, drains
write-through, and clears local caches before preparing the next prefix. It
then restarts the worker while preserving Mooncake and admits only a strict L3
source. All requests are sequential, use `max_new_tokens=1`, and set
`routed_dp_rank=0`.

For the documented 128K capacity check, set
`EXP0_MAX_TOTAL_TOKENS=139264` and run:

```bash
workspace/dense_three_tier_kv_exp0/run_exp0.sh 128k
```

At 20 GB/rank this writes a machine-readable `preflight.json` with
`status: skipped`, stops the worker, and produces no measurements.

## Results and Admission

Generated manifests, runtime files, and results are git-ignored. A result root
contains `environment.txt`, `preflight.json`, per-run `l2.jsonl`/`l3.jsonl`,
the corresponding server logs, `summary.json`, and `summary.md`.

TTFT is measured with a monotonic clock from immediately before HTTP send to
the first streamed event containing non-empty generated text. A measurement is
written only after these checks pass:

- L2: `device=0`, `storage=0`, and host covers every cacheable full page.
- L3: `device=0`, `host=0`, storage covers every cacheable full page, and the
  reported backend is Mooncake.
- Prompt/completion counts, manifest hash, fixed revision, TP/DP/DCP, BF16 KV,
  HiCache policies/layout, L1 capacity, and startup-derived bytes/token all
  match the experiment contract.

The summary requires exactly ten L2/L3 pairs per run and reports median/min/max
across run-level L2, L3, and paired delta medians. Any missing, duplicate, or
wrong-source measurement fails the run instead of entering the summary.
