# K1-ticket05 用户验收确认

2026-10-31，用户明确回复：

> 可以，验收。如果有性能测试在工程文件里，记得删掉

验收版本：`db4b3ac1b926b9274f533f3c8f086bc1cd8034ce`。
运行：31/32，`k1-ticket05-002`，`p-sharing-acceptance`，target/restart两轮。
[002报告](README.md)记录命令、模型、环境、日志路径及用户回传的P汇总和D终端片段。
P汇总含D证据和双端清理；32独立summary未回传，不补造该文件。

按前一轮向用户说明的精简范围接受：两轮各6请求，ABCE/长生成/EOS、内容、Graph、
共享哨兵、实际写计数、ready/free、槽复用、超时fail-stop、同端口重启与双端清理通过。
P主KV注册8份；B/C零主KV写入rank符合连续分块与命中后的后缀位置。
001的TP状态分歧已修复，002未复现；001失败记录继续保留。

全部limitations原样保留：无额外BM DRAM内容读回，V3独立答案，V8实际注册CPU重放，
物理NUMA驻留及性能未验收，退出沿用默认父进程清理。
本次用户确认不把上述限制或MemFabric析构问题改写为已独立验证。
原始JSON的`user_accepted: false`不改写，本文件记录人工确认。

05设为closed。08的03/05依赖已满足，仍等待07自身验收；本次不开展后续票。

## 工程性能代码检查

检查基点`4c0e79b550`至验收提交的13个净功能文件，并搜索Ascend/NPU mempool模块与当前验收入口。
未发现ticket05的性能测试、perf配置、NPU计时event或benchmark调用，无需删除功能代码。
共享哨兵、写入行数、覆盖位图和ready/free证据属于正确性检查，保留。
manager中的NUMA性能选项位负责SDK NUMA ID编码；`registration_seconds=None`说明SDK无独立计时器，均非性能测试。
既有独立组件benchmark工具和SGLang原有profiler不属于本票工程性能埋点，本次不修改。
功能源码及脚本/测试净改动均为零；源码阅读清单见[交付说明](../../delivery-ticket05.md)。

## 本次验证

本次只归档验收和更新状态，没有修改运行代码或重新运行模型。
JSON一致性、12个请求的完整行数/字节数组、分配总量、文档链接和diff检查通过。
此前修复已完成40项相关CPU检查；不重复这些测试，NPU证据仍对应db4b3ac1b9。
Standards与Spec文档复审均0项问题；独立复核未发现本票工程性能埋点。
两份HTML渲染与STE检查通过，34个本地文档链接有效。
