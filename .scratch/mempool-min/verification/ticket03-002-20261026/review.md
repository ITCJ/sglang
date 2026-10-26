# Ticket03 首层诊断 review

基线 `8579aa2bab2e523780f971744001e691985682e8`；审查本轮 7 个源码/测试文件与交付说明。
Standards 与 Spec 由两位独立 agent 只读审查，未纳入用户此前其他票的未提交文档。

## Standards

0 项发现。接线全部位于 Ascend/NPU，服务外预算/摘要职责有说明；阅读顺序与进度页同步。
检查点有每 rank 64 MiB 限制并计入预检，重复数值统计集中到 _difference。

## Spec

0 项发现。真实 DSA prepare/core 与 forward_sparse 接到同一 PrefixStore。
原始 SFA 输出继续进入模型；额外两次算子仅在冷 A 首层执行，C 不额外执行。
尾页调用只截取相同 Q/RoPE/top-k 并调整 query length，保留 KV/页表/KV lengths。
独立 CPU 副本、默认关闭和请求生命周期 guard 有效；缺/重复/串错证据不会标为完整。

## 验证限制

Mac 只验证诊断计算、实际方法接线与返回值保持；测试替换 NPU 算子原语，未复现其数值。
NPU 首层实验由用户运行 ticket03-003；002 完整输出失败保留，根因仍未知。
确认修复并完成 NPU 验收后删除 [DEBUG-ticket03-reuse] 临时探针。
