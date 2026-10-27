---
template: doc
theme: shadcn
mode: light
lang: zh
title: Ticket02：共享 NUMA KV 的实现计划
subtitle: 先贯通共享请求，再证明两处并行，最后交付真实服务证据
source: 2026-10-14 · 代码 d2c999b4fa · 当前工作区规格
---

按 `ask-matt`，本票进入 `/implement` 流程。
01 已关闭，02 可开始。规格和分票已完成。
本页是实现建议；生产共享路径和本票 NPU 验收尚未完成。
验收依据仍是 [ticket02](issues/02-mla-shared-kv-overlap.md)。

## A 实现顺序与关键判断

```timeline v
1 | 生产共享池 | 统一分组、贡献、映射、身份和有序关闭
2 | 正确请求 | 本地 current、共享历史、同层发布、Graph 与释放
3 | selected 并行 | current-fill 与历史 copy 分开提交，在消费者汇合
4 | 写回并行 | indexer 前提交，比较同层 join 与滞后一层
5 | 服务证据 | 内容、token、timeline、初步性能和清理报告
```

**current 的本地供给要在第 2 步实现。**
只改共享地址和 writer mask，无法保证同层读到 peer 的新 KV。
先用本地 current 保证正确，再扩大并行窗口。

代码核对发现四处需要改合同：

| 当前行为 | 共享后需要的行为 |
|---|---|
| manager 固定两个 BM participants | 一个 g 成员组有 2g 个 participants |
| 所有 rank 写对侧同一个 probe | 每个成员独占 probe 区域 |
| 本 rank 写入计数决定完整可读长度 | 分开记录分片写入、本地 current、已发布历史 |
| backend 在 indexer 后写入 | 在 compact KV 就绪后提前提交，backend 消费同一份状态 |

## B 先把 NUMA 配置变成统一计划

在 `mempool/config.py` 解析配置。
建议新增纯数据 `SharingPlan`，放在生产 `mempool/sharing.py`。
它负责分配、分组、owner、participants、端口和身份计算。
CPU 测试直接验证这份计划，manager 只执行已验证的计划。

| 输入或规则 | 实现建议 |
|---|---|
| `SGLANG_NPU_MEMPOOL_SHARE` | 严格解析 0/1；未设置取 0 |
| NUMA 节点列表 | 按本机 rank 升序连续均分，余数给前面的节点（2026-10-22修订） |
| 显式分配 | 拟新增 `--mempool-rank-numa-map <JSON文件>` |
| 映射文件内容 | 使用 host/rank/node 记录数组，明确检查重复和遗漏 |
| 两种分配同时设置 | 启动报冲突 |
| share=1 | 所有节点必须明确且有效；不接受自动节点占位值 |
| share=0 | 同样按连续规则分配 NUMA，每 rank 独立存储 |

映射覆盖本侧全部 TP ranks，并按物理主机校验本机节点。
主机身份来自启动拓扑；容器名和 NUMA 编号不能独立充当主机身份。
P/D 可以使用不同节点编号，但逻辑 rank 成员集合必须一致。

分组键包括模型/TP 实例、PD 侧、主机、NUMA 和 KV 身份。
组内按全局 TP rank 排序，首名为物理 owner。
不相容的 KV 身份明确失败，不能悄悄拆成多份副本。

例如，本机四个 ranks 配置旧列表 `0,2`：

| rank | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| NUMA | 0 | 0 | 2 | 2 |
| share=1 的成员集合 | {0,1} | {0,1} | {2,3} | {2,3} |

这得到两组相邻 ranks 的共享池；非相邻分组仍可用显式映射提供。
启动日志逐 rank 打印 node、group、owner、contribution 和 pool identity。

## C 将实验中的共享合同接入生产 manager

复用01验证过的零贡献和 owner 映射方式。
生产代码通过自身 manager 实现，组件入口也调用生产 manager。
实验专用 `SharingPool` 不成为服务运行时依赖。

| 身份 | g 成员组中的含义 |
|---|---|
| 逻辑 side | P=0，D=1；继续用于 KV view |
| 组内 member | 按成员列表编号 0…g−1 |
| BM participant | `side * g + member` |
| 物理 owner | 各侧 member=0，贡献完整逻辑池 |
| 其他成员 | 贡献 0，各自持有 SDK handle 和设备映射 |

建议把 manager 的初始化推广为按 group 启动。
原 rank-pair 初始化保留为 g=1 兼容入口。
`view(side, layer)` 继续返回逻辑 P/D 视图。
group 计划负责把逻辑 side 映射到实际 owner participant。

**启动和身份必须随分配一起改。**
分配前先汇总本侧 TP 计划，并执行有超时的 P/D 计划交换。
模式、成员、layout、writer 规则和端口冲突在这里失败。
后续 rank-pair 准入握手再核对 group identity 和启动 session。
启动计划交换早于现有服务握手，不能等 BM 分配完成才发现组不一致。

probe 按 member 分区，保留每个 rank-pair 的 session 校验。
在 payload 之后预留足够区域，并计入对齐后的实际贡献。
每个成员只写自己的 probe；映射检查覆盖两侧 owner 的完整层范围。

正常退出沿用01的顺序：

```flow LR
全员设备drain -> 全员pool关闭
全员pool关闭 -> 非Store客户端退出
非Store客户端退出 -> Store owner最后退出
```

生产服务也要有组内退出协调、超时和阶段日志。
BM 和 TransferEngine 共用 MF；MF 退出还需等待其全部用户。

## D 第一条真实请求：本地 current + 共享历史

第一版复用01的连续行分工作为起点。
对当前 forward 的 N 个有效 rows，按组大小 g 均分。
前 `N % g` 个成员各多一行，其余成员可以零写入。
有效 row 序号由设备 metadata 生成，padding 不参加分工。
N=1 时只有一名 writer，报告如实记录这一点。

后续每个 reader 都读取完整 selected KV。
共享减少副本和重复写入，不会自动减少每个 reader 的读取量。

runtime 内部拆开三种事实：

| 事实 | 用途 |
|---|---|
| writer coverage | 校验本 rank 实际完成的分片与预期分片一致 |
| local current | 本次 forward 已生成、可直接填入 selected 的 KV |
| published history | 全组写入已通过发布依赖，可供历史读取的范围 |

第一版在本层已有 TP 前完成本地 writer 依赖。
通信的设备完成依赖建立后，推进相应层的已发布范围。
CPU 提交计数不能直接推进 published history。

current 按每个请求的 logical position 分类。
source row 来自当前 batch row，destination 来自 top-k 中的实际位置。
重复选中 current 时填满所有对应位置；未选中 current 时仍持久化。
保留不同 P/D slots、padding、slot generation 与 attempt 检查。

复用既有全 TP tick 统一 acquire、binding 和 release。
P 的分片完成事实与 native handoff 分别推进。
全部 prompt 分片完成后才发送 KV_READY。
D 同时等待 KV_READY 和 native readiness。
取消与回收覆盖 writer、selected copy、attention、refill 和 handoff。

## E selected 的并行先独立验证

```flow LR
selected清零与topk路由 -> copy_ready
copy_ready -> current填充
copy_ready -> 历史HBM hit
copy_ready -> 历史BM miss
本地compact KV -> current填充
current填充 -> selected_ready
历史HBM hit -> selected_ready
历史BM miss -> selected_ready
selected_ready -> attention
selected_ready -> cache refill
```

current、hit、miss 的 destination masks 互斥且完整。
`copy_ready` 覆盖初始化、binding 和路由 metadata。
它不包含 current-fill，也不包含本步 BM write-done。
attention 和 refill 都等待实际来源完成。
slot-map 更新继续按自身 metadata 依赖执行。

先比较三种 selected 组织，固定同一种写回策略：

| 候选 | 并行关系 |
|---|---|
| 串行对照 | current 填入后再启动历史 copy |
| 复用 hit stream | current 与历史 HBM hit 顺序执行，和 BM miss 并行 |
| 独立 current stream | current 与历史 hit/miss 分开提交 |

用 selected-ready 耗时选择，计入事件和路由成本。
P/D 两种 BM miss 仍沿用现有 miss stream 顺序。

## F 再提前写入，并覆盖下一层 attention

NPU DSA prepare 完成 norm/RoPE/必要重排后，提交 compact KV。
提交点放在 indexer 调用之前，也覆盖 shared-topk 层。
建议由 runtime 返回受生命周期保护的 layer KV 状态。
backend 使用这份状态，保证同一层只提交一次写入。

一次打包的 compact source 同时供 current-fill 和 writer 使用。
每 rank 复用一条持久 writer stream。
单执行 stream 先用两组 source/metadata 交替。
writer scratch 与历史读/refill scratch 分开。

| 候选 | 提交点 | 写回完成边界 |
|---|---|---|
| A：共享串行对照 | 当前 backend | 本层 attention 前 |
| B：stream 诊断 | 当前 backend，writer stream | 本层 TP 前 |
| C：提前提交 | compact-ready，writer stream | 本层 TP 前 |
| D：滞后一层 | compact-ready，writer stream | 下一层 TP 前，末层收齐 |

selected 路径和 writer 分工固定后，再比较这些候选。
B/C 可作为测试诊断选项，不必都成为长期公开配置。

D 的依赖如下，L 是当前层：

| 已有 TP 的位置 | 主执行路径接入的完成事件 |
|---|---|
| TP(0) 前 | 无前驱写入 |
| TP(L) 前 | `write_done[L−1]` |
| 最后一层 TP 前 | 包括末层在内的全部剩余写入 |

这样 `write(L)` 可以覆盖 `attention(L+1)`。
等待具体层事件，不能用 `wait_stream(writer)` 带入当前层写入。
L+2 复用 L 的 staging 前，必须完成 L 的所有消费者。

在 decoder 的已有 TP 调用附近接入小 hook。
先核对实际量化、scatter 和 attention TP 路径。
对应 collective 必须覆盖共享组所有 writers。
不满足此前提的配置明确拒绝，避免把无通信分支当成发布点。

tail flush 放在捕获模型内部、末层已有 TP 之前。
D completion snapshot 排在 flush 和通信依赖之后。
P 完成快照跟随实际 writer stream。
普通 scheduler overlap、异步连续 forward 和换 Graph bucket 都沿用设备依赖。
事件及 staging 按捕获/执行身份保护，首版写回在本 step 内收齐。

## G 代码切片与完成条件

这些切片仍属于02，每片从外部行为测试开始。
硬件结果按阶段记录；所有验收项通过后才关闭整票。

| 切片 | 主要代码位置 | 完成条件 |
|---|---|---|
| 1：分组计划 | `mempool/config.py`、拟新增 `sharing.py`、配置入口 | 旧列表兼容；1/2/4成员、不均匀分配和非法配置 CPU 检查通过 |
| 2：生产共享池 | `mempool/manager.py`、`layout.py`、启动计划交换 | 生产 manager 的 NPU 入口证明零贡献、完整读回、独立映射和有序关闭 |
| 3：保守请求路径 | `runtime.py`、`offload.py`、sparse manager/attention、PD protocol/service/tick | 本地 current、同层发布、真实 eager/Graph 请求与安全复用通过 |
| 4：selected 并行 | sparse manager/attention | 内容一致；current/history timeline 和 selected-ready 对照齐全 |
| 5：提前写回 | NPU DSA module、runtime、decoder/communicator、Graph runner | 同层/跨层对照；延迟 writer、双 staging、tail flush 和连续 replay 通过 |
| 6：真实服务交付 | `ascend-mempool-test` 自动入口及报告 | GLM-5.2、GLM-5.1快速回归、内容、初步性能和清理证据齐全 |

关键代码证据：

- [manager](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py)：两 participant 初始化、映射、probe 和 close。
- [runtime](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：`write_layer`、`selected_kv_valid`、完成快照。
- [DSA prepare](../../python/sglang/srt/hardware_backend/npu/modules/deepseek_v2_attention_mla_npu.py)：compact-ready 到 indexer 的插入位置。
- [sparse manager](../../python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py)：`materialize_selected_kv` 的 hit/miss/refill 依赖。
- [decoder](../../python/sglang/srt/models/deepseek_v2.py) 与 [communicator](../../python/sglang/srt/layers/communicator.py)：attention 后已有 TP 路径。
- [自动服务入口](../../ascend-mempool-test/scripts/run_ticket10.py)：复用启动、协调、请求、报告和进程清理框架。

## H 验收证据与尚待硬件回答的问题

CPU 检查验证配置、身份、分片覆盖和可读范围合同。
测试从公开入口验证行为，保留 independent 路径回归。
不要求默认重跑全部历史性能矩阵。

NPU 按三层推进：

1. 验证生产 manager 的 g=1/2/4 分配和完整访问。
2. 验证 Graph 数据路径、延迟 writer 和复用顺序。
3. 验证 GLM-5.2 TP16 服务及 GLM-5.1 快速回归。

真实模型在所用量化配置下比较组内 compact KV 内容。
同时对照 share=0/1 的短 greedy tokens 和稀疏 selected KV。
请求长度覆盖大于 index_topk=2048 的历史范围。
Graph 覆盖 zero-writer、padding、重复 replay、bucket 切换和重绑。

timeline 区分21个 producer层和57个 shared-topk层。
报告小/大 batch 的 selected-ready、写回重叠和 TP 前剩余等待。
另报 tail flush、写读争用、打包与事件成本、吞吐和 ITL。
profile 与计时分开采样，保留原始数据。

复用自动框架，交付每机/容器一条命令。
新入口名和 CLI 随实现交付，本页不提供未实现的运行命令。
日志统一保存到 `/home/cryang/log/`。
报告区分 smoke、完整验收、请求完成和清理完成；失败返回非零。

**02 需要真实并行证据和初步性能结果。**
最终默认策略及逐负载3%门槛由 [03](issues/03-performance-acceptance.md) 验收。
01 的物理页落点仍保留 `unverified`，不影响本次规划开始。
BM 跨 rank 可见性与部署环境的跨 stream Graph 行为由本票硬件验证。
本轮只核对代码并形成计划，未运行 CPU 功能测试或 NPU 测试。

参考：[执行设计](execution-design.md)、[发布 ADR](../../docs/adr/0001-mla-shared-kv-publication.md)、[阶段交付流程](../ascend-mempool/verification.md)。
