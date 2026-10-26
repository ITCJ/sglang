# Ticket03 临时诊断 review

基线 `0308cd081418f6bf789f7cf5b623741d22965132`，只审本轮 8 个源码/测试文件及交付说明。
两位独立 review agent 分别核对 Standards 与 Spec；没有审入用户既有其他票文档。

## Standards

最终 0 项发现。服务接线全部位于 Ascend/NPU；服务外入口与汇总器的必要性已说明。
默认关闭、保留一页、功能源码阅读顺序和目标页 D 进度均符合要求。

## Spec

初始 1 项 P2：摘要仅数 hit_len 与层数，会把缺 baseline/hash/checks 或串错 rid 的证据
标为 complete。修正为核对 A baseline、A/C 输入身份、C/reference_rid 以及必需检查项。
六个负向变体先失败后通过，另有真实探针输出到终端摘要贯通正例。
最终复核原 P2 已解决，0 项未解决。

## 限制

这是诊断代码审查，不能证明 NPU 模型精度已修复。旧 run 的 C 输出失败保留；
本轮 --diagnose-reuse 仍待用户在双机执行。严格输出相等的验收条件未改变。
模型问题解决并完成硬件验证后，删除 [DEBUG-ticket03-reuse] 临时探针。
