# Agent Instructions

## Agent skills

### Issue tracker

Issues and specs live as local Markdown under `.scratch/<feature>/`.
See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default five triage labels as local `Status` values.
See `docs/agents/triage-labels.md`.

### Domain docs

Read `CONTEXT.md` when reviewing Ascend sparse KV history. Use a single-context
layout as described in `docs/agents/domain.md`.

### Ascend mempool delivery

When implementing, handing off, or closing an Ascend mempool ticket, read
`.scratch/ascend-mempool/verification.md` for stage review and user-run NPU
verification. Hardware acceptance gates ticket closure and dependent work.

### Autonomous tests

Deliver future tests through a self-running entry point that automates setup,
service readiness, requests, result checks, report collection, and cleanup of its
own processes. For multi-host tests, one command per host/container is acceptable;
coordinate subsequent steps automatically. Save NPU test logs and reports under
`/home/cryang/log/`, return nonzero on failure, and distinguish smoke coverage from
full acceptance. The user runs hardware tests unless access is explicitly granted.
