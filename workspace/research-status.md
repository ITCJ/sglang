# Research Status

Updated: 2026-09-16.

## Goal

Study efficient KV cache management on an Ascend A3 supernode. The current
system reference is SGLang HiCache, but the original HiCache + Mooncake Fabric
combination is not a supported setup.

## Established facts

- The environment is two Ascend 910C A3 machines, Ubuntu 22.04 arm64,
  CANN 9.0.0, and host driver 26.1.1.
- Mooncake tests using Mooncake-allocated Fabric memory passed locally and
  across the two nodes. These tests did not independently identify the
  physical HCCS route.
- SGLang HiCache cannot register its PyTorch pinned Host memory with the
  Mooncake Fabric path (`-600`, `aclrtMemRetainAllocationHandle`). People
  familiar with the setup confirmed that this combination is outside the
  supported target setup. This does not mean that Mooncake Fabric itself is
  unsupported on A3.
- Host `ib_write_bw` tests did not produce a valid RDMA transfer result.
  Ordinary TCP Store testing reached `TCP:REMOTE_OK`, but the full HiCache TCP
  path was not completed and TCP is not a performance baseline for the
  supernode interconnect. The Ascend Mooncake build needs `MC_FORCE_TCP=1` to
  bypass automatic Ascend transport installation when running the TCP probe.
- A TP16+DP1 model smoke test and chat request passed. TP16+DPA16 capacity and
  the formal experiment have not been validated.
- Machine 28 went down during the test period. Whether the test caused the
  outage has not been established; no result from that period is treated as a
  stable performance measurement.

## Current direction

The immediate experiment is the independent KV path benchmark in
[kv-path-plan.md](kv-path-plan.md). It uses Mooncake Store + Fabric and keeps
the HiCache page object granularity while comparing `L2→L1`, `L3→L2→L1`, and
`L3→L1`. It does not call the HiCache storage adapter or load a model.

A preliminary, code-reading-only assessment of an experimental A3 HiSparse +
Mooncake Store + Fabric baseline is recorded in
[a3-hisparse-mooncake-fabric.md](a3-hisparse-mooncake-fabric.md). It is not a
validated implementation design and must not be treated as evidence that the
combined stack works.

The benchmark plan is a working research plan and should be updated here only
when the research direction changes. Environment instructions belong in
[ascend-setup.md](ascend-setup.md); detailed test evidence belongs in
[fabric-note.md](fabric-note.md).

## Scope boundary

The benchmark can compare the specified data paths under the chosen Fabric
implementation. It cannot by itself establish that native HiCache is slow or
that a new cache manager is better. Any future HiCache Fabric adaptation must
be identified as an adaptation and compared with the same underlying transfer
capabilities.

## Performance measurement revision

The 80 formal rows from kv_path_bench/260915_110547 and
fabric_direct_bench/260915_104705 are withdrawn from performance analysis.
Reruns use whole-request submission and wall-clock samples, without a fixed
page batching requirement or sums of independently warmed batches. Staging is
excluded. These reruns are not yet validated on the remote NPU. Native HiCache
L2-only work is handled separately; see kv-path-plan.md for ownership and scope.

## Next Step: Verify UNIDEX BM Local and Remote Paths

Earlier inspection missed an external mapping layer: [remote benchmark `f934478`](https://github.com/hibikid/ascend-ub-bench/blob/f934478756ab5be92cfe409a3f6bc3baaf4b207f/remote_dram_sparse_copy_bench.py#L764-L785)
maps a MemFabric remote Host GVA to `LOCAL_DEVICE` and passes it to the existing
UNIDEX `src_ptr`. The [MemFabric whole-request suite](fabric_direct_bench/README.md#unidex-bm-映射补充实验)
now has optional UNIDEX paths for BM local Host and remote Host to the same
final L1. The separate SysV local entry remains available and distinctly
labeled. SysV was a deliberate local allocation choice; omitting BM mapping
from that first entry reflected an earlier information gap. L3's K-then-RoPE
per-page layout and L2's separate K/RoPE pools were already part of the
experiment; this change adds the missed BM mapping and corresponding address
adaptation. The new paths preserve 61-layer BF16 separated K/RoPE pages, both
physical mappings, whole-request samples and completion synchronization;
static mapping and fixed index preparation are reported outside timing.
Small-scale explicit data validation precedes default-unvalidated performance
in the remote handoff. No target A3 result is available yet, so existing
research conclusions remain unchanged. The colleague's single-layer 576-wide
top-k mean timings cannot be compared directly to these request median/p95
results.
