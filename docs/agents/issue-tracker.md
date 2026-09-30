# Issue tracker: Local Markdown

Issues and specs for this checkout live as Markdown files in `.scratch/`.
Local Markdown is the configured tracker for engineering skills.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- The authoritative feature spec is `.scratch/<feature-slug>/spec.md`.
- Implementation tickets are one file per issue at
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` in
  dependency order. Keep each ticket in its own file.
- Record the triage role near the top as `**Status:** <role>` using the
  vocabulary in `triage-labels.md`.
- Record `**State:** open` or `**State:** closed` separately from triage.
  A `ready-for-agent` spec is ready for planning; it does not mean the feature
  has been implemented.
- Append comments and work history under `## Comments`, including changes,
  checks actually run, their results, and remaining issues.
- Reference blocking tickets by feature-local number/title and relative link
  under `**Blocked by:**`. A ticket can start when every blocker is closed.

## Skill operations

- Publish a spec: create or update `.scratch/<feature-slug>/spec.md` and apply
  the requested triage role in its `Status` field.
- Publish tickets: write one file per approved ticket under the feature's
  `issues/` directory, with its acceptance criteria and blockers.
- Fetch: read the referenced spec or ticket and its appended comments. Resolve
  ticket numbers within the referenced feature.
- List: inspect the feature directories and ticket files in `.scratch/`.
- Comment: append to `## Comments` in the relevant file.
- Label: update the local `Status` field.
- Close: set `State` to `closed` after acceptance passes, and append completion
  notes with actual verification evidence.

## Wayfinding

When using the wayfinding flow, its map is `.scratch/<effort>/map.md`, with one
child ticket per file under `.scratch/<effort>/issues/`.
Use `Type` for `research`, `prototype`, `grilling`, or `task` and its flow-specific
`Status` values `claimed`/`resolved`. Record blockers in each child file.
Append resolved decisions and child links to the map. An unresolved child can
start when all its blockers are resolved.
