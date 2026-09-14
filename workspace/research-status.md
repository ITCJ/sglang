# Research Status

Updated: 2026-09-14.

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
