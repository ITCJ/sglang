# Ticket03-006 review

Baseline: `1c0280132c5f3085847aae3f7f7ef7951165848f`. Scope: new post-attention probes and controls, offline summary, runner integration, tests and delivery records.

## Standards

Final unresolved: 0. Model hooks live in Ascend/NPU directories. Offline orchestration/summary directory exceptions and functional reading order are documented. Input snapshots precede aliasing operations; replays do not replace model outputs. CPU budgets and diagnostic failure reporting preserve strict acceptance.

## Spec

Final unresolved: 0. Initial P2: ModelSlim uses `layer.scheme`, not only `quant_method.scheme`; the manifest initially omitted actual scheme/kernel settings. Fixed with actual object links, private Tensor scales and Mapping digests; red/green negative test and reviewer reproduction passed. Real decoder hook boundaries, original in-place reduction aliasing, TP1 dense MLP replay context, token alignment and controlled-input isolation were reviewed.

CPU tests establish diagnostic behavior only. User-run NPU 006 is pending. Ticket03 stays open and ticket04 remains blocked.
