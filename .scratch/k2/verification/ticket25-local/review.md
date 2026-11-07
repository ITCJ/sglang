# Ticket25 review

Base: 46af27553847c013f07bad5c2428b1c5460a082b. Reviewed worktree diff and new files before commit.

## Standards

Three judgment findings fixed: removed unused retained request bindings; documented the fixed source-table ordering; reused remote_mask for both observation counters. No generic source/shutdown changes. Final static unresolved: 0.

## Spec

Four findings fixed: PoolLayout slots access/support contract; suffix-only candidate reference verification; actual remote mask and per-layer candidate counts/bytes; physical read-source observation. Final static unresolved: 0.

Reviewers did not execute NPU or CPU tests. Separate agents reviewed the two axes. CPU service results do not prove installed kernel/Graph/remote visibility. User excluded dual-write on hardware group; ticket remains open.
