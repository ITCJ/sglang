# MLA NUMA 共享：实施会话入口

更新于2026-10-14。用户明确接受ticket01完成，并要求核对02/03安排。
本文件是进度入口；需求以spec、执行设计与独立ticket为准。

## 当前可执行工作

- [01组件实验](issues/01-dual-rank-pool-benchmark.md)已由用户接受并关闭。
- [02共享接入与并行](issues/02-mla-shared-kv-overlap.md)解除阻塞，可开始实现。
  生产SGLang共享路径尚未实现；本轮只更新规格与安排。
- [03性能验收](issues/03-performance-acceptance.md)仍等待02用户NPU验收。
  [原04](../ascend-mempool/issues/04-single-request-graph-decode.md)等待共享03。
- 物理NUMA页落点证据仍缺失，`numa_verified=false`不改写为true。
  用户接受此限制关闭01，不需要再次确认，也不主动重开延期的原09调查。

## 阅读顺序

1. 根AGENTS.md、CONTEXT.md；核对git status与最新提交。分支为`cryang/dev/mempool`。
   本地设计/tickets尚未commit/push，换checkout前需要携带，不要覆盖既有改动。
2. [规格](spec.md)、[执行设计](execution-design.md)、[交付计划](delivery-plan.md)。
   配置设计以执行设计第0节为准；事件依赖保留[ADR0001](../../docs/adr/0001-mla-shared-kv-publication.md)。
3. [02](issues/02-mla-shared-kv-overlap.md)是当前实施范围，
   [03](issues/03-performance-acceptance.md)给出下一阶段边界。
4. [阶段交付与NPU验收](../ascend-mempool/verification.md)。硬件仍由用户运行。
5. 需要历史依据再读[设计讨论](design.md)、01 Comments及
   [原会话入口](../ascend-mempool/SESSION-RESUME.md)，无需重新访谈已确认决定。

## 最新设计决定

| 决定 | 规范位置 |
| --- | --- |
| 默认每NUMA两个TP ranks；先分配NUMA，再按share开关决定池数量 | spec、执行设计第0节 |
| share=0每rank独立；share=1同机同节点同逻辑compact KV一池 | 02配置/身份验收 |
| 组成员数按实际分配生成，至少验证1/2/4及不均匀分配；不跨主机/节点合池 | 02组件接入 |
| 新共享部署模板开启share；未设置开关保持原独立模式，改变配置需重启 | 执行设计第0节 |
| 2026-10-22改为按每台主机的rank升序连续均分NUMA列表；余数给前面的节点；显式映射保留 | 执行设计第0节 |
| P/D逻辑组成员相同，节点编号可不同；一名物理owner，其他成员零贡献 | spec、02 |
| 所有rank保留handle与本地设备映射，owner不转发copy | 02映射/关闭合同 |
| bulk共享合计写一份、目的范围互斥；每个reader仍读完整top-k | 01结果、02内容验收 |
| 真实模型TP16不变；1/2/4为共享组成员数，默认每侧8份compact KV | 02/03 |
| 当前KV本地填selected，与历史copy独立提交，attention/refill汇合 | 02、执行设计第2节 |
| 提前BM写回可覆盖同一步下一层attention，末层Graph内flush，不跨step | 02、ADR0001 |
| metadata/source复用等全部消费者，保留普通scheduler overlap | 02生命周期 |
| P保留native HBM，BM全写完才KV_READY；D还等native handoff | 02请求闭环 |
| 21个producer与57个shared-topk层分别测量；78层compact KV仍独立 | 02/03 |
| 正式对照固定NUMA分配只切share；吞吐/ITL P50/P95分别不超过3%回退 | 03 |
| 原P/D miss双stream优化在原04，Index K去重及3M/2T容量上限不在本轮 | spec范围 |

“可调节”按启动时配置理解；不要求在线benchmark、自迁移或动态重建池。
独立模式兼容默认值与新共享部署默认值已分别注明。上述新配置尚未实现。

## 01证据位置

最新C/D代码：61e22c8603；启动对齐修复0d000b262a；收尾修复d2c999b4fa。
均已push到origin/cryang/dev/mempool。Mac完整354项CPU测试及适用静态检查通过；
用户运行`cd-v1-resume2`，两侧493/493且收尾完成，约18.3分钟（续跑时长）。
远端日志：`/home/cryang/log/numa-cd/cd-v1-resume2/{prefill,decode}/`。

C/D固定Graph/48，一层16×65536×576 BF16，TP1/2/4共七种布局，三轮。
主写与等总copy量分开，读为每rank相同索引完整top-k2048、batch1/16，
local/remote/mixed独立施压。162行CSV每行60批；全部NUMA标记未验证。
[完整报告](visuals/cd-v1-resume2/report.html)同目录有CSV、复现脚本及图表。

36MiB主写：TP2共享约0.529ms，TP4共享约0.522ms；TP4集中独立约2.081ms。
等copy量TP4集中独立/共享均约0.522ms；收益主要来自减少重复写。
TP4 batch16读取集中独立/共享差异不足0.4%；分散配置本地读明显提高。
这些数字是组件结果，不能替代真实服务吞吐或NUMA物理页证明。

已解决的测试bug、旧run-1至run-10与续跑证据保存在01 Comments；不重复推进
历史失败。正常关闭必须保留pool关闭、客户端退出、Store owner最后退出的协调。

## 新会话提示

```text
$ask-matt 读取 .scratch/ascend-mempool-numa-sharing/SESSION-START.md、spec.md、
execution-design.md和02 ticket，以及.scratch/ascend-mempool/verification.md。
01已由我明确接受关闭；默认每NUMA两个TP ranks，NUMA分配后用share开关合池。
核对当前代码和工作区后衔接02。NPU由我运行，03等待02真实服务验收。
```
