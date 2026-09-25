# Issue tracker: GitHub

Issues and specs for this checkout live in `ITCJ/sglang` GitHub Issues.
Use `gh` with `-R ITCJ/sglang` for tracker operations. The `upstream`
remote is not this project's issue tracker.

## Operations

- Create: `gh issue create -R ITCJ/sglang --title "..." --body-file <file>`.
- Read: `gh issue view <number> -R ITCJ/sglang --comments`.
- List: `gh issue list -R ITCJ/sglang --state open --json number,title,body,labels`.
- Comment: `gh issue comment <number> -R ITCJ/sglang --body-file <file>`.
- Label: `gh issue edit <number> -R ITCJ/sglang --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number> -R ITCJ/sglang`.

When a skill says "publish to the issue tracker", create an issue here.
When it says "fetch the relevant ticket", read the issue here.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Wayfinding

A map is one issue labelled `wayfinder:map`; its child tickets are GitHub
sub-issues when available, otherwise linked in the map's task list.
Use native issue dependencies for blockers when available; otherwise
record `Blocked by: #<number>` in the child issue.
