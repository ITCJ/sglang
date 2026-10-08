# Ascend MemFabric Mempool Sparse KV PD Demo

**Type:** spec
**Status:** ready-for-agent
**State:** open

**Current stage (2026-10-07):** Tickets 01–03 are accepted and closed, including
user-confirmed S6 NPU acceptance of the final cutover cleanup. The user requested
[10: GLM-5.2 algorithm adaptation](issues/10-glm52-indexer-sharing.md) before
continuing [04](issues/04-single-request-graph-decode.md): execution order is
03 → 10 → 04, followed by the existing downstream dependencies. Ticket09 remains
deferred. The overall feature remains open.

**Historical review (2026-09-28):** [Design and code review](design-review-2026-09-28.md)
records the findings that informed subsequent work. Unapproved proposals there do
not override the confirmed spec or later ticket decisions.

## Problem Statement

Ascend sparse KV PD disaggregation currently transfers prompt KV through MemFabric
TransferEngine into decode-side staging and then into decode-side host storage.
Within one superpod, we want a demo that keeps prompt KV in prefill-side DRAM and
lets decode fetch only the KV selected by the indexer through MemFabric BM
mappings and UniDexCopy. Newly produced decode KV should reside in decode-side DRAM.

The challenge includes request ownership: prefill KV remains on P throughout
decode, so ordinary handoff cleanup cannot make its storage reusable. The demo
must establish readiness, synchronize slot acquisition across TP ranks, preserve
bindings across sender/receiver cleanup, and release storage only after outstanding
NPU reads and writes have drained. Successful eager copies alone are insufficient:
the complete decode path must support NPU Graph capture/replay and preserve accuracy.

## Solution

Implement an opt-in Ascend/NPU mempool mode using 16 independent two-rank BM pools
between two 16-NPU machines in the same superpod. Each P rank owns prompt KV;
its paired D rank owns decode KV and accesses both sources through typed logical
KV views managed by an Ascend/NPU manager.

Retain P's native HBM KV cache for prefill attention and directly offload the
kernel's temporary compact KV to P mempool in parallel with that native write.
Retain SGLang's HBM Index K management and its existing PD state/aux/metadata
transfers. Replace the main compact-KV transfer and staging with sparse reads
from the two BM sources. Reuse the existing PD control transport and add persistent
request ownership, binding, readiness, cancellation, and release handling.

The initial deliverable is one complete GLM-5.1 request with decode graph replay,
followed by sequential requests, slot reuse, failure-path validation, and an AIME26
comparison against the existing sparse PD TransferEngine baseline.

## User Stories

1. As an inference operator, I want to enable mempool mode explicitly alongside sparse KV offload, so that I can select the intended PD storage path.
2. As an inference operator, I want incompatible devices, PD backends, roles, and peer configurations to fail clearly, so that a partial configuration cannot silently select an incorrect KV path.
3. As an inference operator, I want fixed P rank i to D rank i pairing on two TP=16, PP=1 machines, so that the first demo has a reproducible deployment topology.
4. As an inference operator, I want each paired rank to use its own two-rank pool and predictable store port, so that initialization and failures can be traced to one pair.
5. As an inference operator, I want configurable prompt and decode token capacities, so that I can run both the default demo and longer accuracy evaluations.
6. As an inference operator, I want independent P and D physical DRAM contributions, so that unequal prompt and decode limits do not force equal physical allocations.
7. As an Ascend developer, I want pool mappings and stable graph buffers ready before capture, and peer layouts validated before request admission, so that captured graphs use valid addresses and incompatible peers cannot serve requests.
8. As an Ascend developer, I want a typed mempool KV view with shape, dtype, and logical indexing, so that callers do not manually find rank base pointers and calculate byte offsets.
9. As an Ascend developer, I want model-derived, layer-based KV layouts with alignment and kernel-range checks, so that an accepted configuration fits the actual memory and copy interfaces.
10. As an inference operator, I want mempool to replace the old persistent host KV allocation, so that the same KV is not stored twice in DRAM.
11. As an Ascend developer, I want native HBM prefill KV and direct temporary-buffer offload to coexist, so that prefill attention remains correct while prompt KV reaches mempool.
12. As an Ascend developer, I want temporary KV buffers kept alive until their offload completes, so that asynchronous copies cannot read reused source memory.
13. As an inference operator, I want prompt KV to remain unchanged on P while decode uses it, so that remote sparse reads always retrieve the request's data.
14. As an Ascend developer, I want newly produced decode KV written to D mempool, so that generated tokens have a local backing store.
15. As an Ascend developer, I want selected KV positions routed to P or D using prompt length and actual KV write progress, so that the prompt/decode boundary is correct.
16. As an inference operator, I want Index K and required state/aux/metadata to retain their existing management and transfer, so that the demo preserves indexer and handoff correctness.
17. As an Ascend developer, I want main-KV transfer and its staging disabled selectively, so that mempool does not leave redundant traffic or remove Index K transfers.
18. As an inference operator, I want D to acquire its slot before requesting a P slot, so that each admitted request has decode-side storage.
19. As an Ascend developer, I want P and D to acquire independently and bind potentially different slot IDs, so that ownership does not assume identical allocators.
20. As an inference operator, I want all ranks on one side to acquire the same slot for a request, so that distributed forwards address consistent request storage.
21. As an inference operator, I want capacity checked consistently before rank-wide acquisition and unexpected partial acquisition to stop service, so that divergent ownership cannot continue into a distributed forward. Ordinary capacity shortage waits without partial allocation; safe cancellation rollback remains supported.
22. As an inference operator, I want prefill admitted only after every P rank has acquired and D has confirmed the binding, so that computation cannot overwrite live prompt KV.
23. As a request client, I want legal requests without available slots to wait within the existing PD bootstrap timeout, so that capacity pressure has bounded behavior.
24. As an inference operator, I want readiness to require readable prompt KV and successful Index K/state/metadata transfer, so that decode cannot start with incomplete inputs.
25. As an Ascend developer, I want prompt and decode cache misses copied through separate UniDexCopy calls, so that each selected row comes from the correct BM source.
26. As an Ascend developer, I want attention to wait for both copy sources and relevant writes, so that stream overlap preserves data dependencies.
27. As an inference operator, I want decode capture/replay with graph batch width at least 16, so that the GLM-5.1/DeepEP configuration is supported.
28. As an Ascend developer, I want replay to use changing slots, lengths, indices, and masks with fixed buffer addresses, so that one graph works for successive requests.
29. As an Ascend developer, I want padded graph rows excluded from physical KV access, so that a single real request can use the graph without touching another slot.
30. As an inference operator, I want sparse cache state reset when request or mempool slots are reused, so that a new request cannot obtain an old request's cache hits.
31. As an Ascend developer, I want ownership to survive handoff sender/receiver cleanup, so that active decode remains connected to its prompt KV.
32. As a request client, I want disconnects, aborts, timeouts, and transfer failures to cancel pending or active work, so that abandoned requests stop consuming resources when safe.
33. As an inference operator, I want release to wait for the final submitted NPU reads and writes, so that finished output does not cause premature slot reuse.
34. As an inference operator, I want D to acknowledge completion and P to acknowledge release, so that prompt storage is reclaimed through an explicit completion exchange.
35. As an inference operator, I want duplicate and delayed messages handled using session, request attempt, and slot generation identity, so that old traffic cannot change a new owner's slot.
36. As an inference operator, I want unsafe slots to remain unavailable when peer completion cannot be established, so that connection loss cannot cause an overwrite.
37. As a request client, I want requests outside prompt, decode, context, or HBM capacity to receive a clear error, so that unserviceable requests are not queued indefinitely.
38. As an inference operator, I want a demo request requiring retraction to terminate explicitly through cancellation, so that unsupported recovery does not silently discard remote bindings.
39. As an evaluation engineer, I want BM/UniDexCopy eager and graph results checked against expected KV element by element, so that copy correctness has a direct reference.
40. As an evaluation engineer, I want fixed short greedy generations compared with the existing sparse PD baseline, so that integration regressions are observable.
41. As an evaluation engineer, I want an AIME26 comparison with identical weights, requests, and inference settings, so that the demo demonstrates no accuracy regression.
42. As an Ascend maintainer, I want implementation concentrated in Ascend/NPU modules with necessary shared hooks kept small, so that the demo respects the project's upstream boundaries.

## Implementation Decisions

### GLM-5.2 Algorithm Adaptation (2026-10-07)

The user supplied a 78-layer `GlmMoeDsaForCausalLM` configuration with 21 full
indexer layers and 57 shared layers, plus a W8A8/W8A8_DYNAMIC/FLOAT quantization
manifest excerpt. Ticket10 adds support and verification for this concrete model
before ticket04. Explicit `indexer_types` controls top-k production/reuse; shared
layers reuse token positions while retaining their own compact KV at every layer.
Compact Index K storage/transfer follows producer layer IDs, independently of the
78-layer BF16 mempool layout. The model's declared weight dtype does not select
KV quantization. Initial acceptance uses non-speculative TP16/PP1 decoding;
NextN/MTP inference is not implied by configuration fields.

Verify actual sparse selection above index_topk=2048, eager/Graph results,
same-weight ordinary sparse PD versus mempool token sequences, normal release,
and GLM-5.1 compatibility. Existing GLM-5.1 requirements remain in force.
Ticket10 is an added delivery requirement; P/D miss parallelization remains a
separate ticket04 proposal, and NUMA investigation remains deferred in ticket09.

### Demo Priority and Deferred NUMA Investigation (2026-10-02)

The user chose to continue the functional demo and defer further NUMA/allocation
debugging to [ticket09](issues/09-numa-allocation-followup.md). It records node-specific
HAL failures, native cleanup crashes, large-allocation latency, existing workarounds,
and all available evidence. It does not block tickets 02–08 and is not an additional
demo acceptance gate. Deferral does not establish that these problems are fixed.

Ticket02's actual top-k BM readback and normal lifecycle checks were accepted by
the user on 2026-10-03 using the small-capacity profile and explicit NUMA list
`0,2,4,6`: context1024, prompt/decode capacities512, TP16, and graph batch width16.
Continue with ticket03, which connects verified BM reads to attention and removes duplicate old
hostSHM, main compact-KV transfer, and staging while retaining the required HBM
cache/Index K and auxiliary transfers. Ticket04 verifies the formal Graph/model path.
Larger-capacity allocation investigations should use the final storage configuration
when resumed; actual KV correctness and ownership/drain requirements remain in force.

### Ticket 02 Staging

The user confirmed a shadow integration stage before replacing the ordinary sparse
PD storage path. Run real GLM-5.1 P/D servers, preserve native P HBM KV writes,
main-KV transfer, D staging/hostSHM and attention reads, and additionally write
temporary KV to P/D BM. Keep P BM ownership through D drain/DONE even though the
ordinary transfer has completed. Tests may use smaller capacities, including the
working 512-token-per-side service profile; configurable storage defaults remain 16384.

First verify ordinary server operation with shadow writes and the real control
lifecycle, without BM readback. After the user's NPU confirmation, add independent
UniDexCopy reads of the actual selected top-k KV and compare valid contents against
the existing path. Readback is a verification buffer, not attention input. Both
steps are required to complete ticket 02. Removing duplicate persistent DRAM and
main-KV transfer remains a subsequent integration step in the final demo.

### Ticket 02 Review Optimizations and Part IV (2026-10-01)

The user confirmed two implementation sections in [ticket02](issues/02-rank-pair-control-lifecycle.md):
first optimize the existing parts I–III, then implement part IV. Both are implemented,
and the real-service hardware acceptance was confirmed on 2026-10-03. The following
contracts remain the basis for subsequent integration.

- Separate transient request-row attachment from persistent mempool slot ownership.
  In the shadow stage, KV_READY alone cannot release P native HBM KV or request rows:
  ordinary staging/transfer may still read them. Normal cleanup also requires native
  handoff success, completed local operations, and no future submissions using the row.
  Consume its completions and retain immutable progress before detaching/reusing it.
  The P mempool slot remains owned until the matching DONE and P write-safety checks
  permit the unified tick to release it. D still drains before detach/release.
- Project real SGLang requests through the Ascend service using `req.kv.req_pool_idx`;
  keep Req/fake/protocol interpretation out of the device runtime. Remove the unused
  fixed-input writer interface and retain explicit slots/positions/valid metadata.
- Expose immutable control snapshots. Control remains the sole owner of protocol
  phase/allocation state; service stores integration facts and pending native cleanup,
  while runtime reports device progress. Only the unified tick commits ownership changes.
- Retain the eight existing production modules. Add only Ascend `mempool_service.py`
  and `mempool_tick.py`; do not add integration or queue-subclass layers. Use the seven
  explicit shared-file hooks listed in ticket02. Run tick at the end of
  `Scheduler.ingest_requests()`, before the PD loops' paused branch; keep capture/replay
  hooks in the NPU graph files and a thin eager scope in ModelRunner.

### Backend Runtime and Shadow Service Delivery (2026-09-29)

- Both P and D access their process's mempool runtime through `AscendAttnBackend`.
  Mempool must not depend on constructing or retaining `SparseKVCacheManager`.
  Ownership decisions remain with the Ascend control layer and common TP tick.
- Integrate shadow writes at the actual temporary compact-KV forward boundary.
  Follow existing sparse UniDexCopy device-index and stream patterns, adapting
  bindings and token positions to the BM layout. Check MLA preprocessing and
  `save_kv_cache` paths for missed or duplicate writes.
- Preserve fixed graph metadata addresses with ordered in-place replay updates.
  Capture/fake/padded rows are invalid; real unbound requests must not silently
  pass as dummy work. Preserve source lifetime and avoid overlap metadata races.
- The desired first real-server gate requires part III plus necessary part IV
  startup, configuration, control, scheduling, drain and fault hooks. Backend-only
  capture does not prove real requests wrote to BM. Keep the code review batches
  distinct while testing the integrated shadow lifecycle.
- A standalone raw-destination writer graph/content gate complements ticket01's
  fetch gate. The first real-server gate checks startup, warmup, capture/replay,
  real binding/writes/release and normal output without in-server BM readback.
  Only subsequent top-k readback establishes the requested KV content comparison.
- Final removal of the old sparse manager requires preserving/migrating its
  still-needed HBM sparse-cache and materialization functions. That cutover is
  outside this shadow stage.

Confirmed file responsibilities and outstanding implementation checks are in
[the current design entry](design.md) and the two sections of ticket02.
The 2026-10-01 module organization is approved; its implementation remains pending.

### Deployment, Configuration, and Startup

- Reject mempool mode combined with MLAPO at startup; mempool writes must not rely solely on `save_kv_cache`.
- Use the existing local GLM-5.1 weights. KV is BF16; this does not prescribe the weight dtype.
- Use two 16-NPU machines in the same superpod, TP=16 and PP=1, with fixed P rank i to D rank i pairing.
- Create 16 independent BM pools. Within each pool, P is rank 0 and D is rank 1. P starts the store and D connects to P's base port plus i.
- Require `SGLANG_NPU_ENABLE_MEMPOOL=1` together with the existing sparse KV offload switch. Validate NPU, Ascend PD backend, PD role, and peer compatibility before admitting requests.
- Preserve the existing non-mempool implementations in the same revision. With mempool disabled, ordinary sparse PD must still allocate its original storage and execute main compact-KV transfer, D staging-to-host copy, D host KV writes, and sparse host-SHM reads. This requirement is separate from retaining ticket02 shadow mode.
- Resolve the storage/transfer mode at startup before allocation and graph capture. Derive resource and execution choices from that mode; readback does not select the data source. Changing modes requires draining and restarting P/D. Reject incompatible peers or missing BM dependencies in formal mode; do not fall back to host storage. Non-mempool operation must not require BM initialization or MemFabric runtime dependencies.
- P's native-prefill sparse offload mode does not itself enable host offload. Resolve mempool configuration explicitly; do not infer that the new mode is disabled from the existing host-offload property.
- Set physical `B_slots=16` on each side. Expose configurable `S_P` and `S_D` server arguments, both defaulting to 16384. Graph batch width and physical slot count are separate quantities.
- Establish BM pools, validate local-device mappings on all D ranks, and allocate/initialize fixed graph buffers before decode graph capture. Capture and warmup use invalid request slots and masks, so they do not read live KV. Keep handles and mappings alive until all captured work has drained.
- Once the existing PD bootstrap provides the peer ZMQ endpoint, validate peer protocol version, role/rank, pool session, dtype, layer layout, logical capacities, physical contribution sizes, and common address stride before admitting the first request. This control handshake does not gate graph capture.

### Mempool Layout and Manager Contract

- Divide the rank's allocation into layer slabs. Each layer has logical batch-slot, token, head, and compact-KV dimensions; derive layer count and KV dimensions from the actual model. Current MLA uses one logical head and includes latent KV plus RoPE key in the compact row.
- Compute each side's required bytes from all local layers, 16 physical slots, its token capacity, model KV row dimensions, and BF16 element size, including alignment.
- Use BM `create2` with separately aligned local contributions and the same maximum DRAM size on both peers. The common maximum is the larger aligned contribution; it defines the rank address stride rather than requiring both peers to contribute that much physical DRAM.
- Respect the selected backend's allocation alignment. The 910C GVA_V4 VMM DRAM backend requires 1 GiB alignment.
- Supply a stable base pointer per layer to UniDexCopy. Enforce its current limits: each source/destination logical span fits `UINT32_MAX`, and each row is at most 32 KiB. Reject unsupported configurations at startup; the total rank pool may exceed 4 GiB.
- In formal mempool mode, skip the old persistent host KV allocation/mapping and use BM-backed storage. Retain the old allocation code for ordinary mode, the existing sparse HBM cache, and Index K; do not create duplicate long-lived DRAM copies of the same KV in formal mode.
- Provide a manager and typed logical KV views that own pool lifetime, shape/dtype/stride, mapped addresses, bounds, and logical-to-copy indexing. Callers supply logical layer/slot/token coordinates rather than manually discovering rank base pointers.
- Distinguish GVA from current-process device VA. Translate BM peer addresses to local-device mappings for kernel access; another process's device pointer is not a valid wire-level address. Remote CPU access is not promised by the logical view.
- Maintain explicit mappings between SGLang request-pool rows and mempool slots. Track the actual amount of written KV independently of output token count.
- Route positions inside the prompt to the bound P slot; route later positions to the bound D slot after subtracting prompt length. P's first sampled output token obtains KV only when D processes it in a subsequent forward.
- Admission must fit prompt/decode capacities, model context limits, and remaining HBM requirements, including Index K and P's native KV. DRAM capacity alone is insufficient. Reject unserviceable lengths clearly; wait for temporary slot pressure.

### Data and Graph Integration

- P acquires and binds before scheduling prefill. Keep optimistic prefill disabled.
- Retain native HBM KV writes for P attention and add direct offload from each forward's temporary compact KV to P mempool. Do not reread the entire native cache at prefill completion to populate BM.
- Preserve temporary source-buffer lifetime across asynchronous offload. Publish `KV_READY` only after all prompt writes complete and the data is readable by D.
- Preserve existing Index K, state, aux, and handoff-metadata management/transfers. Enumerate their actual buffers during integration so disabling main compact-KV transfer cannot also remove Index K.
- Disable only main compact-KV transfer and its decode staging in this mode; retain the legacy implementations behind startup-mode selection. Exclude main K/V from the formal transfer buffer list without removing P's native HBM cache. Sender/receiver handoff cleanup and P native-page release remain separate from persistent BM ownership.
- Decode readiness requires both matching `KV_READY` and successful existing Index K/state/metadata transfer, across the relevant ranks. Their arrival order is immaterial.
- Reuse the sparse HBM lookup/hit/refill path. Select the miss source by startup mode: the existing host-SHM implementation in ordinary mode, or P/D BM in formal mode. Split formal misses into P prompt and D decode sources, and use separate UniDexCopy calls for the two layer views. D directly offloads newly produced compact KV into its BM portion; formal mode neither submits legacy host writes nor reads legacy host KV.
- Parallel copy streams may be used when destination rows do not conflict; attention waits for both sources and relevant writes. Correctness is the requirement, not a prescribed performance gain.
- Require decode NPU Graph capture/replay with minimum graph batch width 16. Keep pool addresses, layer bases, HBM cache addresses, and metadata buffer addresses fixed throughout graph use.
- Update slot bindings, lengths, batch mappings, indices, and valid masks through fixed device tensors. Capture both source paths even if one has no valid rows during capture.
- Padded rows neither acquire physical KV slots nor access real request storage. Reset sparse cache mappings on request-row and slot reuse. Replay cannot retain the first request's host-side slot or length constants.

### Control Protocol and Rank Coordination

- Reuse existing PD bootstrap and ZMQ endpoints/sockets. Intercept tagged mempool messages in the Ascend manager's existing receive path; keep one receiver per PULL socket.
- Maintain a persistent acquired-request table independent of transient sender/receiver handoff tables. Correlate requests through their shared bootstrap room, with pool session/epoch, request attempt, and slot generation checks. Local request IDs are diagnostic rather than the sole cross-side identity.
- D acquires first, then asks P to acquire independently. Each side uses one common slot ID across its 16 ranks, but P and D slot IDs may differ.
- Confirmed D1 (2026-09-29): use one control tick per scheduler iteration, after request intake and before the paused branch, on each side's complete TP CPU group. This is state progression, not a periodic reset. P and D coordinate independently; no cross-side collective is required. Accept first-version per-tick CPU metadata synchronization, including empty inputs.
- Align candidates using common `(room, attempt)` identities and persistent observations; validate pair-local sessions/proofs locally. Updated by user approval on 2026-10-07: prepare complete local control transitions before the observation all-gather, and include their validation results and admission claims. Form a common plan using these results, available slots and record capacity, then commit and gather commit status. These are two fixed collectives, including empty ticks; no separate post-plan preflight collective is required. Send generated replies and admit model work only after all ranks report success. Receive/result callbacks stage facts rather than releasing slots independently.
- Synchronize both acquire and release, including safe rollback. Capacity shortage waits before allocation. Only compose prepared actions whose request/slot dependencies do not conflict; conflicting actions wait for a later tick. Retain pair-local identity, binding, generation, retirement, phase and ownership checks, without copying unrelated request history. Unexpected partial acquisition after successful common preparation is fatal and must not resume serving through rollback/retry. Ordinary request cancellation retains safe rollback semantics. P may schedule prefill only after all P ranks are acquired and the D binding is confirmed.
- D releases its slot only after all D ranks have drained and invalidated old binding references; send DONE after successful rank-wide release. P releases only after all P ranks have corresponding DONE and completed P writes; send RELEASE_ACK after successful rank-wide release. D may reuse its released slot while retaining the old completion record until acknowledgement.
- See [D1/D4/D5 design](d1-d4-d5-design.md). D4 conservative drain and D5 fatal-error policies are confirmed below; their runtime hooks and fault-detection details still require implementation.
- Receive threads parse and queue state changes; they do not block for free slots, run collectives independently, or mutate release order asynchronously.
- Keep the following message meanings. Exact wire encoding and implementation names can follow local conventions while preserving this contract.

| Message | Direction | Required meaning |
| --- | --- | --- |
| `POOL_HELLO` / `POOL_READY` | D to P / P to D | Exchange startup identity, version, rank/role, dtype/layout, logical capacities, and local/common allocation sizes; confirm control compatibility before request admission. |
| `ACQUIRE` | D to P | Request identity, acquired D slot/generation, prompt/decode limits, and reply routing; enqueue one P acquisition attempt idempotently. |
| `ACQUIRED` | P to D | Confirm successful P acquisition and the P/D slot identities/generations. |
| `BOUND_ACK` | D to P | Confirm the full binding; P admits prefill only after rank-wide prerequisites hold. |
| `KV_READY` | P to D | Confirm completed, readable prompt KV for the binding and its actual length. |
| `CANCEL` | Either way | Identify the request/known binding and reason; stop new work and begin cancellation. This is not proof of drain. |
| `DONE` | D to P | Confirm no further D reads/writes for the attempt after drain, including normal completion or cancellation. |
| `RELEASE_ACK` | P to D | Confirm the exact allocation no longer holds P resources, whether retired by DONE or safe rollback; close a matching pending completion record. |

- Preserve existing transfer Success/Failed and abort messages. Existing `ABORT_ACK` has different drain semantics and does not replace the decode-completion `DONE` exchange.
- Validate every message against identity and generation. Duplicate messages are idempotent. A duplicate completion for a released binding cannot free a new occupant of the same slot.
- Retain sufficient terminal identity to reject late acquisition/binding messages and answer repeated completion messages. Retiring terminal records must preserve generation protection.

### Admission Hooks (Confirmed 2026-09-30)

- Retain D1's complete observations, common ownership decisions, commit-status
  synchronization and empty-input participation. The 2026-10-07 preparation
  update above replaces the original separate preflight round; MIN-only and
  idle-skip optimizations remain deferred.
- P checks the tick-approved acquire/binding state at the beginning of
  `finalize_bootstrap`, before metadata allocation or sender initialization.
  Return False to keep waiting when not approved. No local ownership mutation
  belongs in this hook; disable optimistic prefill and audit alternate callers.
- D gates transfer-queue admission on both the original transfer/metadata/staging
  readiness and tick-approved mempool readiness. Cover both metadata-only and
  staging poll paths; continue staging progress while waiting for KV_READY.
- Record original transfer completion independently of the final admission gate,
  so the tick can observe completion without waiting for its own approval.
  Preserve failure handling and receiver-independent bindings; verify both
  readiness arrival orders.
- Use a separate mempool pending-release list, integrated with scheduler idle,
  leak-check and sleep decisions without changing health-check semantics blindly.
  Never inherit the old deferred-release timeout's forced-free behavior. Combine
  approved releases into one drain where possible and measure the pause cost.
- Ticket02 includes basic fatal-error handling needed by the integrated service;
  ticket07 adds systematic fault scenarios, not the first safety hooks.

### Ownership State Machines and Failure Behavior

These are semantic states, not a required representation or enum implementation.

| Side | State | Transition condition |
| --- | --- | --- |
| P | `WAITING_ACQUIRE` | All ranks successfully acquire the same P slot. |
| P | `ACQUIRED` | Receive matching D binding confirmation. |
| P | `BOUND` | Rank-wide ownership, binding, and original input metadata permit prefill scheduling. |
| P | `PREFILLING` | All prompt offloads complete, visibility is established, and KV_READY is published. |
| P | `WAITING_DONE` | Receive matching D drain confirmation and establish that P has no outstanding writes. |
| P | `RELEASED` | Slot is reusable; retain necessary terminal identity and send/repeat release confirmation. |
| D | `WAITING_ACQUIRE` | All ranks acquire the same D slot. |
| D | `ACQUIRED` | Obtain the matching P slot binding. |
| D | `BOUND` | Record the mapping and send BOUND_ACK. |
| D | `WAITING_READY` | Both prompt readiness and existing transfers succeed across ranks. |
| D | `DECODING` | Generation finishes or cancellation stops future scheduling. |
| D | `DRAINING` | Every submitted graph, copy, and offload that accesses this request has completed. |
| D | `WAITING_RELEASE_ACK` | D slot is already released and DONE has been sent; retain only necessary control state. |
| D | `CLOSED` | Receive matching P release confirmation. |

- Slot-owning nonterminal states may enter `CANCELLING`. After binding, cancellation follows D drain, DONE, P write drain/release, and RELEASE_ACK. Before binding, acquisitions that cannot have been accessed may roll back safely while retaining cancellation identity.
- Once a slot has been released, old control records only finish acknowledgements; they cannot operate on its new owner.
- Confirmed retirement semantics: ordinary unbound CANCEL still performs rollback without an extra ACK round trip. A subsequent authenticated DONE for that safely retired allocation can receive RELEASE_ACK, both before and after terminal-record eviction.
- Record a per-slot retired generation only on actual release, including safe rollback. Combine this boundary with startup sessions, the signed request/P/D allocation proof, and current ownership checks. A generation number or a missing request record alone does not prove retirement.
- Keep recent request records bounded; do not retain one permanent proof per completed request or impose a cumulative request limit. Every new generation of a slot requires its previous owner to have released it.
- Charge acquire waiting to the existing PD bootstrap timeout. Moving between queues or retrying acquire must not restart the timeout. Router timeout, client disconnect, explicit abort, or transfer failure may cancel earlier.
- Connect cancellation to both new queues and persistent ownership. Handoff cleanup, HTTP completion, allocator free, elapsed time, and CANCEL alone do not establish NPU drain.
- Include already-submitted extra forwards from overlap scheduling in D drain. Keep P's BM slot until D explicitly confirms completion and P's own writes have finished.
- Confirmed D4 (2026-09-29): defer finished requests' ordinary KV/request-row release, collectively pause new D batch submissions, flush existing overlap results and delayed sampling, and complete device work before invalidating old bindings and reporting drain to the unified tick. Retain unfinished requests' storage/state and resume their scheduling afterward. Cover normal completion, cancellation, and zero-decode completion; fake warmup/capture inputs do not acquire real slots. The first version accepts this pause cost.
- Confirmed D5 (2026-09-29): unrecoverable mapping, protocol, or TP ownership errors report an error and terminate service rather than recover in-process. Coordinate errors across the local TP group when possible; dead/hung ranks require bounded timeout/watchdog handling. Error/timeout is not DONE and cannot authorize unconfirmed slot reuse or BM teardown. Local exception handling alone does not prove remote reads have stopped; cross-side failure detection and coordinated shutdown instructions remain integration requirements.
- Track peer dependence throughout active decode, including after the ordinary receiver is cleared. Report peer/rank failure and stop unsafe accesses. Unconfirmed P slots remain unavailable; timeout does not authorize reuse.
- Do not support automatic retraction/rebootstrap in the demo. Use admission limits; if a request still requires retraction, terminate it explicitly through cancellation. Do not interpret request-pool release as remote KV completion.
- Concentrate layout, manager, protocol, offload, and fetch logic in Ascend/NPU modules. Shared server arguments and lifecycle hooks are limited to what integration requires and must preserve ordinary modes.

## Testing Decisions

The user has confirmed these observable acceptance boundaries during design.
Use the existing PD service request/response interface as the primary integration
boundary. Use the existing BM/UniDexCopy benchmark boundary for hardware correctness
and graph feasibility. This spec does not require a new public service testing API.

Tests assert KV contents, outputs, protocol readiness, and observable ownership
behavior. They should not depend on private table layouts, method-call counts, or
a particular enum/class decomposition. Focused protocol tests may drive the manager's
message/operation boundary and observe whether slots can safely serve a later request.

| Area | Required checks |
| --- | --- |
| Startup and storage | Two real paired machines; independent local contribution sizes; matching common stride; valid mappings; deterministic rejection of incompatible layouts, flags, capacities, or copy ranges. |
| BM and UniDexCopy | Element-by-element expected BF16 KV for both source portions in eager and capture/replay; changed indices, zero-valid sources, boundary positions, and padded rows. |
| Complete PD request | Acquisition, binding, P dual writes, retained Index K/state/metadata transfer, decode graph replay, D drain, DONE, P release, and acknowledgement with GLM-5.1. |
| Replay and reuse | Consecutive requests, changed lengths and slots, repeated physical slot/request-row use, unequal P/D slot IDs, and no stale sparse cache data. |
| Readiness ordering | Both KV_READY-before-transfer-Success and the reverse; no decode until both are ready; failures cancel rather than admitting decode. |
| Capacity and cancellation | Slot-pressure waiting, bootstrap timeout, disconnect/abort while waiting or active, unexpected partial-rank acquire fail-stop, safe cancellation rollback, overlapping work at completion, and no reusable slot until drain. |
| Protocol identity | Duplicate and delayed acquisition/binding/completion, cancellation before late acquisition, old session/attempt/generation messages, and no release of a new owner. |
| Peer failure | Active-decode peer failure is observable after handoff cleanup; unsafe accesses stop and unconfirmed P storage is not reused. |
| Output correctness | Fixed short greedy token sequences match the ordinary sparse PD TransferEngine baseline using identical weights and inference settings. |
| Accuracy | AIME26 score does not decrease with identical weights, requests, and inference configuration; any discrepancy is investigated before accepting the demo. |
| Existing modes | Focused regression verification of ordinary sparse PD/offload behavior at any shared integration hook changed by the feature. |

Prior art includes the existing remote DRAM sparse copy benchmark with complete
content verification, UniDexCopy graph coverage in ordinary sparse PD, existing
PD polling/rank synchronization, and the current GLM-5.1/AIME26 evaluation flow.

The AIME26 flow currently uses temperature 0 and a maximum of 28672 output tokens.
Raise `S_D` to cover that configured upper bound and validate total context/Index K
HBM capacity. The default 16384 decode limit does not cover this evaluation setting.

The first hardware gate must establish the combined BM remote-pointer plus NPU Graph
path. Reported eager BM results and ordinary-path graph replay are separate evidence;
neither proves the combined path. Record throughput and latency after correctness
passes; no fixed performance improvement is an acceptance condition.

## Out of Scope

- P prefill graph capture/replay.
- Prefix reuse, speculative/draft decoding, and automatic retraction/rebootstrap.
- Automatic reconnection, request migration, or forced reclamation of slots whose drain is unconfirmed.
- General P:D routing ratios or dynamic rank pairing beyond the fixed demo topology.
- NUMA-group shared KV deduplication, including the future one-copy-per-NUMA MLA design.
- Support for arbitrary model KV layouts beyond the selected compact MLA demo.
- Removing P's native HBM KV cache or making prefill attention consume remote/full-context BM KV directly.
- Moving Index K into mempool or changing its full-context indexer access strategy.
- Extending UniDexCopy's address/row limits for unsupported large per-layer configurations.
- Model weight acquisition, unrelated mainline refactoring, and a promised performance target.

## Further Notes

- Treat commit `295132c4a5`, the initial upstream Ascend sparsity-driven KV offload merge in PR #33089, as this project's development baseline. The active feature branch is `cryang/dev/mempool`.
- The user reports successful, fully checked sparse/dense remote DRAM benchmark runs on the intended machines. Ticket 01's combined remote BM fetch Graph and asymmetric contributions were accepted on 2026-09-27. Ticket 02 subsequently passed the two-machine writer/Graph gate (20 PASS checks per side), small-capacity real-service TP lifecycle, and selected-KV readback with physical slot reuse; the user confirmed acceptance on 2026-10-03. Formal attention cutover and its accuracy verification remain with tickets 03–08.
- This is the parent feature spec. Subsequent tickets should declare blockers and deliver observable slices: the hardware graph gate, one complete PD request through release acknowledgement, request reuse/cancellation coverage, and accuracy acceptance.
- Preserve explicit storage-owner and consumer identity and independent P/D slots, so that future NUMA sharing and different serving ratios do not depend on slot equality.
- This local spec defines intended behavior; implementation and hardware acceptance have not been completed by publishing the spec.
