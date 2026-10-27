# MLA NUMA 共享：交付计划

**Type:** delivery plan
**Status:** ready-for-agent
**State:** open

更新于2026-10-14。用户已接受01完成；默认每NUMA两个TP ranks，通过启动时
共享开关决定池数量。需求以[规格](spec.md)为准，配置与事件依赖见
[执行设计](execution-design.md)。本轮调整安排，尚未实现生产共享路径。

| Ticket | 当前状态 | 交付行为 | 前置条件 |
| --- | --- | --- | --- |
| [01](issues/01-dual-rank-pool-benchmark.md) 组件读写对照 | closed，用户接受 | TP1/2/4 C/D、三种池布局、内容与Graph验证、结果报告 | 已完成 |
| [02](issues/02-mla-shared-kv-overlap.md) 共享接入与并行 | open，可开始 | 可配置分组、真实GLM-5.2 PD/Graph、分工写与完整读、安全释放、并行时间线 | 01已关闭 |
| [03](issues/03-performance-acceptance.md) 策略与性能验收 | open，等待02 | 默认双成员部署的同负载吞吐、ITL与物理贡献对照 | 02用户硬件验收 |

01的物理NUMA页落点仍未验证，原始结果保留pending标记。用户接受该限制关闭
组件票；02/03仍需真实服务的独立硬件证据。完整结果见[报告](visuals/cd-v1-resume2/report.html)。

## 02：接入真实模型并证明并行正确

按三个可验证步骤推进，仍归同一票：

1. NUMA分配与share开关贯通实际请求生命周期。share=0每rank一池；share=1
   同机同节点同逻辑KV一池，owner与每进程映射分开。默认双成员，1/4成员
   及不均匀分配由同一分组合同处理。
2. current-fill与历史copy在selected处汇合；BM写回提前提交，并比较同层
   完成与覆盖下一层attention。Graph内事件、staging复用和尾部flush均验证。
3. 提供GLM-5.2 TP16真实请求、GLM-5.1快速回归、同负载初步性能和timeline。
   用户验收内容、Graph、去重、释放及并行行为后关闭02。

新共享部署模板显式开启share，旧启动未设置时保持独立池。先解析NUMA再分组，
不会把相邻rank或固定八组写死。修改分配/开关在重启后生效；在线迁移不在本轮。
详细验收项集中在[02](issues/02-mla-shared-kv-overlap.md)。

## 03：固定布局验收真实服务性能

主比较固定同一rank→NUMA分配、NPU、CPU亲和性与逻辑容量，只切换share=0/1。
默认每节点两ranks时，每侧payload从16份变8份。写入分工、selected并行和
写回调度的影响分别解释，最终方案按代表负载交替多轮采样。

每个负载要求decode吞吐不低于基线97%，ITL P50/P95不高于基线103%。统计
原始流式token时间；实际pool贡献、写入字节和完整读取字节单独报告。
NUMA分配改变或四成员共享另列诊断，不能混入默认双成员结论。

复用01有效结果与02服务入口；改动影响组件行为时补回归，不默认重跑493项。
最终门槛与回退方法见[03](issues/03-performance-acceptance.md)。

## 执行关系

01已关闭 → 02 → 03 → [原mempool ticket04](../ascend-mempool/issues/04-single-request-graph-decode.md)。

NPU由用户运行，每机/容器一条自动命令，覆盖准备、readiness、请求、检查、
报告和清理。提供阶段进度与收尾超时，日志统一写入`/home/cryang/log/`。
代码交付与CPU通过不替代用户硬件验收；遵循[交付流程](../ascend-mempool/verification.md)。

## Comments

2026-10-14：用户接受01，确定每NUMA双rank默认值和分配后合池开关。
更新02为可变成员的共享接入，03固定布局做正式性能验收；保留三票边界。
以下2026-10-12记录是历史安排，状态以上表和各票最新记录为准。

2026-10-12：按用户对三阶段的反馈完成分票。当前可开始01，02/03被硬件验收
阻塞。本次只完成方案、验收设计与本地 tracker 发布，没有实现或 NPU 性能结果。

同日按用户补充细化 selected 并行填充，以及同一步下一层 attention 与
write-back 重叠；不跨 token 的边界已由用户明确确认。

同日用户最终确认“初步设计完成了”。`grill-with-docs → to-spec → to-tickets`
阶段已完成；实施入口为01双 rank 独立／共享池读写对照，之后按02→03→原04
推进。三个实现 tickets 保持 open；每阶段仍需实际实现、检查与用户NPU验收。
