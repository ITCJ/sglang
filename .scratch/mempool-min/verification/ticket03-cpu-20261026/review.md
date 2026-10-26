# Ticket03 implementation review

Base: `87157b7365a78aa336d9558e1ce9f820bee2fdd4`.
The two reviewers inspected the working diff and all new source/test files.
No NPU acceptance is claimed. The ticket remains open.

## Standards

Initial findings: 1, resolved: 1, final unresolved: 0.

The current target-alignment summary still described ticket03 as unimplemented,
despite the completed implementation and CPU evidence in section D.
This violated the AGENTS.md requirement to distinguish implementation, remaining
work and user-accepted NPU verification. The status, prose and ticket03 row were
updated; Markdown and HTML were regenerated and rechecked by the reviewer.

No functional source violated the Ascend/NPU placement requirement. The delivery
document lists the functional reading order and explains why the existing test
harness owns the external runner/checkers. No baseline smell required refactoring.

## Spec

Final unresolved findings: 0. No scope expansion was found.

Reviewed continuous ready-prefix matching, B/C hit caps, native token indices,
flat/NZ layout, complete Index K transfer, independent fill/suffix completion,
source protection, TP consensus and native release before fill. The actual
Req -> extend -> ChunkCache cleanup path preserves request ownership.

The reviewer confirmed two corrections found during parent review: the write
range snapshot precedes the completion event; local read receipts may repeat
while waiting for a late rank, while Main ack/unlock remains exactly once.
Their regression tests and the final full CPU suite passed.

Standards: 0 unresolved; Spec: 0 unresolved. NPU acceptance remains pending.
