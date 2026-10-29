# 已验收性能基线：结果归档

本目录保留已验收的测试结果及其历史CPU检查记录。
性能实现仅保存在`cryang/dev/mempool-min-baselinetest`（`134b22d90d`）。
用户2026-10-29要求工程分支仅保留结果，因此不包含性能工程代码、运行入口或重算脚本。

- `baseline01-002/acceptance.json`：用户确认验收及原自动波动failed记录。
- `baseline01-002/analysis.md`、`derived.json`、`metrics.csv`、`round-samples.csv`：报告与采样结果。
- `baseline01-001/diagnosis.md`：首次失败与诊断记录。
- `cpu-20261028/`、`core-answer-v1.md`：当时版本的CPU验证和输出策略验证记录。

这些文件中的命令、文件路径及审查结论描述的是当时的性能测试分支，
不是当前工程分支的可运行入口或验证结果。
性能复测与重算请使用baselinetest分支；历史结果不证明K1当前版本的性能。
