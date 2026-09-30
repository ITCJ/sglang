# Ascend Sparse KV Offload Context

## Project Baseline

Treat commit `295132c4a5` (`[NPU] Add sparsity-driven KV offload for DeepSeek
DSA on Ascend`, PR #33089) as this project's initial upstream development
baseline when reviewing its history and changes.

## Glossary

- **Graph batch width**: The number of request rows in a captured decode graph,
  including padded rows. It does not determine how many requests have KV storage.
- **Mempool KV slot**: A bounded physical region reserved for one request's KV
  on one side of PD disaggregation. It is distinct from SGLang's `req_pool_idx`;
  the P and D sides may assign different slots to the same request.
- **Acquire**: An attempt to assign an available mempool KV slot to a request;
  `acquired` means the assignment succeeded. P keeps its slot acquired until D
  explicitly confirms that its final reads have finished.
- **Prompt KV**: KV produced from a request's prompt during prefill. It remains
  owned by that request while decode can still read it.
- **Decode KV**: KV produced by decode forwards for generated tokens. A sampled
  output token does not have KV until a subsequent forward processes it.
- **Index K**: The keys used by the DSA indexer to select token positions. They
  are distinct from the compact MLA KV read by sparse attention.
- **Mempool KV view**: A typed logical view of KV stored in a mempool. It exposes
  tensor shape and element meaning without implying CPU access to remote data.
- **Mempool mapping ready**: BM mappings and stable device addresses/buffers are
  ready for graph capture. It does not imply that the PD control handshake is complete.
- **Mempool control ready**: The paired P/D control peers have completed their
  startup checks and may admit real requests. This is separate from mapping readiness.
- **Bound**: A request whose P and D slot assignments have been confirmed by
  both sides. Binding does not imply that the request's KV is ready.
- **KV ready**: Prompt KV is fully written and readable by D. This does not by
  itself establish readiness of Index K or handoff metadata.
- **Handoff**: The transition from prefill to decode, including Index K and
  request metadata. It is distinct from decode completion or P slot release.
- **Drain**: Completion of every outstanding operation that can read or write
  a request's KV. Finishing generation does not by itself establish drain.
- **Request attempt**: One instance of a request's acquisition and binding.
  Messages from an older attempt cannot change a current slot's ownership.
