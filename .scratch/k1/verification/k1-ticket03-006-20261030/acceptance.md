# K1-ticket03 用户验收确认

2026-10-30，用户明确回复：

> 可以，验收，并且开始清理测试的代码

确认版本：`0337b9b9adb48baf160495594ec9b28908e890ba`。
运行：31/32，`k1-ticket03-006`，`split-read-acceptance`，两轮、两策略。
命令、环境、双端汇总及性能解释见[006报告](README.md)。
日志根目录：`/home/cryang/log/mempool-min/k1-ticket03-006/`。

按已约定的功能范围验收：两策略gather/native HBM与独立源快照精确一致，
读取量、全rank完成门控、六项输出、账本、fail-stop、重启及双端清理通过。
保留报告的全部limitations：无额外BM验证读取，直接DRAM内容、物理NUMA与性能未验收，
V3/V8沿用精简范围，退出沿用默认父进程清理。

K1票03已关闭；不改原始summary的`user_accepted: false`，此文件记录人工确认。
08的03依赖已满足，05/07仍需各自验收；本次不开展后续票。

用户同时授权按交付清单清理临时计时、工程证据探针和gather额外比较。
清理后的CPU检查另行归档；不将006的硬件结论自动扩展为清理后版本已在NPU重测。
