# Ticket03 configuration digest fix review

Baseline: `7229eb70f0`. Scope: full configuration hashing, manifest-local alias cache, diagnostic error reporting and regression tests.

## Standards

Unresolved: 0 (hard violations 0, possible smells 0). Streaming retains all canonical JSON content and uses standard escaping; scratch tracks ordering references. Alias caching does not survive a manifest. Each rank error remains available even when console output groups repeated causes. Directory placement, reading order, D38–40 and hardware gate documentation satisfy AGENTS.md.

## Spec

Unresolved: 0 (missing/partial 0, scope creep 0, incorrect implementation 0). Real ModelSlim scheme/configuration paths use the new digest; A/C independently hash all settings. Failure summaries remain incomplete and retain raw rank records. Documents do not interpret 006 missing data as equality or CPU tests as NPU success.

487 CPU tests and 15 targeted tests passed. The original NPU numerical mismatch remains unresolved; user-run 007 is pending.
