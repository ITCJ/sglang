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

### Ascend implementation and review

Keep Ascend feature changes in `ascend/` or `npu/` directories where possible.
For every functional change outside those directories, document why it is needed
and why an Ascend/NPU hook cannot provide the behavior. On delivery, list the
functional source files in the order the user should review them, with each
file's purpose; exclude tests and README/explanatory files from that list.
This reading order supplements the skills' Standards and Spec code reviews.

If the user explicitly authorizes implementation before a dependency's NPU
acceptance, record the exception and preserve separate commits for hardware
verification. Keep each ticket open until its own NPU acceptance is confirmed;
the exception applies only to the work the user authorized.

### mempool-min progress (temporary)

For Ascend NPU mempool-min work, read section D of
`.scratch/mempool-min/target-alignment.html`; the user uses it to review progress
and which design requirements are implemented. After every implementation step,
immediately update section D in `.scratch/mempool-min/target-alignment.md` and
regenerate the HTML. Record completed work, remaining work, and validation status,
distinguishing implementation from user-accepted NPU verification. This rule
applies only to mempool-min and may be removed after the user confirms acceptance
of the complete feature.

### Autonomous tests

Prefer one commit per ticket, including its implementation, tests, and delivery notes.
Keep repeated NPU acceptance runs to at most two rounds. Reduce model restarts and
duplicate checks; record any reduced coverage explicitly in the report.

Deliver future tests through a self-running entry point that automates setup,
service readiness, requests, result checks, report collection, and cleanup of its
own processes. For multi-host tests, one command per host/container is acceptable;
coordinate subsequent steps automatically. Save NPU test logs and reports under
`/home/cryang/log/`, return nonzero on failure, and distinguish smoke coverage from
full acceptance. The user runs hardware tests unless access is explicitly granted.
