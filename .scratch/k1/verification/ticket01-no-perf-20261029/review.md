# 移除perf后的两轴审查

审查比较原K1提交`3156916a11`与移除后的工作树，并与`8869c2c605`核对保留的功能增量。
用户2026-10-29的要求覆盖原规格中将perf实现接入工程分支的条款。

## Standards

0项硬性违反，0项实质smell finding。
仅保留NUMA映射、注册/驻留证据与双映射功能验收。
性能埋点、perf模式及报告工具已移除；output_streamer和无关Ascend模块恢复原样。
独立验收脚本的目录例外已说明。历史结果未冒充当前验证。

## Spec

0项发现。
性能配置、CLI、专用测试已移除，无悬空模块引用。
numa-acceptance依次运行默认/交换映射的完整功能验收，
保留applied、驻留证据和双端清理；失败返回非零，user_accepted保持false。
baselinetest仍为134b22d90d；真实NPU功能验收待用户执行，ticket保持open。

总计：Standards 0项；Spec 0项。审查覆盖代码工作树，提交历史已单独核验，证据见history-audit.json。
