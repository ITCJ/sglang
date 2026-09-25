# Ascend Sparse KV Offload Project Record

Treat commit `295132c4a5` (`[NPU] Add sparsity-driven KV offload for DeepSeek
DSA on Ascend`, PR #33089) as this project's initial upstream development
baseline when reviewing its history and changes.

When modifying Ascend sparse KV offload or sparse PD code in this repository,
read `../agent-mission-track/sglang-npu-develop.md` for the current project
status and recent change history.

After completing each independent logical code change in this area, and before
the final response, update the progress section when its conclusions changed
and append one entry to the document's Agent Change Log. Record the exact model
identifier and reasoning effort supplied by the runtime, the behavior changed
and why, affected files, checks actually run and their results, and remaining
issues. Write `not provided` for model details the runtime does not expose.
Never infer model details or claim checks that were not run.

Do not append a change-log entry for read-only analysis. Keep unrelated SGLang
work out of this project record.
