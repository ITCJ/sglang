---
title: Ascend NPU mempool-min：阶段 0 四张 ticket 回顾
subtitle: 从冷请求到 prefix 复用，再到双机完整验收
template: doc
theme: blueprint
lang: zh
date: 2026-10-23
status: 设计与实施方案回顾；四票均待实现和 NPU 验收
baseline: 3883624810 + 当前工作区
---

**这四张票交付 Ascend NPU 上的 sparse 基础请求流程。**
顺序是：冷请求与释放复用 → 保存 prefix → 命中恢复 → 完整验收。
当前没有一张完成；01 可开始，02–04 等待前票验收关闭。
本文结合此前关于 Router、SR、scheduler、page、NUMA 和启动的问题展开。

## A 四张票各自交付什么

总体目标来自 syncfile 的 [mempool-min-demo/walkthrough.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/walkthrough.md)。
本阶段范围以本地 [spec.md](spec.md) 和四份独立 ticket 为准。
后续目标和当前进度统一在 [目标页 D 部分](target-alignment.html) 核对。
本文是回顾说明，不能代替实施或硬件证据。

| Ticket | 完成后新增的能力 | 主要新增工作 | 独立验收结果 |
|---|---|---|---|
| [01 冷请求与显式释放](issues/01-cold-request-lifecycle.md) | Main/SR 驱动真实推理并安全复用槽 | 新控制链、实际资源预留、就绪与释放接管 | 两个不同冷请求正确；第二个复用实际归还的槽 |
| [02 保存 prompt 到 prefix](issues/02-prompt-prefix-save.md) | 请求结束后留下可复用数据 | P alive Index K、prefix 页池、目录与 admit | alive 释放后，prefix 主 KV 与 Index K 仍正确 |
| [03 命中恢复与后缀计算](issues/03-prefix-restore-and-suffix.md) | 命中后只计算后缀 | load_back、fill、逐 token 映射、两段完成跟踪 | B 命中 4096；C 命中 3968；完整输出正确 |
| [04 完整 demo 自运行验收](issues/04-full-demo-acceptance.md) | 双机可重复执行完整用例 | 参考组、目标组、负向场景、报告与清理整合 | A/B/C/E 与 V1–V10 在最终集成版本通过 |

**依赖：01 → 用户 NPU 验收关闭 → 02 → 验收关闭 → 03 → 验收关闭 → 04。**
01–03 各自就要有自运行测试入口。
04 汇总完整场景，并修复集成暴露的问题。
数据正确性和生命周期检查从所属 ticket 开始交付。

本阶段的共同配置如下：

| 项目 | 固定范围 |
|---|---|
| 后端与模型 | Ascend NPU；GLM-5.2；主 KV 与 Index K 为 BF16 |
| 部署 | P、D 各一台 16 卡机器，各自 TP16、attention DP1、PP1 |
| 执行 | 普通非投机路径；单 chunk；同时最多一个真实请求 |
| 输入 | token IDs、greedy、prompt 按 B 对齐、生成上限 128 |
| 原生计算 | P eager；D 验证 Ascend Graph capture/replay |
| alive | 每侧每 rank 16 槽；S_P=8192、S_D=128 |
| prefix | 从 02 启用；P 机每 rank 一份，每份 40 个逻辑页（5120/B） |
| 原生缓存 | 关闭原生 radix cache；关闭 sparse decode admit |

模型权重身份和量化配置要随报告记录。
KV 为 BF16 不表示模型权重必须采用相同 dtype。
16 个存储槽、16 个 TP rank、Graph 的 16 行是三个不同数量。
它们都不能证明真实并发已经达到 16。

**当前代码基础与缺口**

已有 NPU mempool 写回、P/D 两处取数、原生传输和 scheduler 接入。
现有复制路径使用 NPU UniDexCopy，runtime 使用 NPU 完成事件。
这些是复用基础；新控制链仍需本轮硬件验证。
拟新增的 Main/SR 模块和 run_mempool_min 入口目前尚不存在。
四票全部为 open，验收项均未勾选。

## B Main Router、SR 和 scheduler 怎样分工

Main Router 是独立 CPU Python 进程，拟默认部署在 P 机。
SR_P、SR_D 随两侧推理进程加载。
每侧 rank 0 scheduler 放置一个 SR 协调者。
每个 rank 都有 SR 执行分片，包括 rank 0。
因此每侧是 1 个协调者加 16 个分片；分片无需单独启动进程。

```flow LR
客户端 -> MainRouter: token IDs
MainRouter -> SR_P协调者: P 侧控制
MainRouter -> SR_D协调者: D 侧控制
SR_P协调者 -> P侧16个分片: tick 内分发与汇总
SR_D协调者 -> D侧16个分片: tick 内分发与汇总
P侧16个分片 -> D侧16个分片: 原生 Index K 与 handoff
```

最后一条边表示保留的 P_i/D_i 原生通道。
实际传输由已有推理与传输模块完成。

| 角色 | 负责决定或执行什么 | 不应混淆的边界 |
|---|---|---|
| Main Router | 入口检查、跨侧配额、目录、命中、操作编排和源保护 | 保存不透明描述符，不计算设备地址 |
| SR 协调者 | 选择本侧槽或 prefix 页、下发一致操作、汇总全部 rank 完成 | 本侧一个 rank 完成不足以回成功 ack |
| SR 分片 | 校验本地绑定、解析映射、执行复制、检查设备完成和归还 | 分片是执行职责，不代表只存 1/16 的主 KV |
| Scheduler | 接收原生请求、推进队列、选择 batch、调用 forward、处理结果 | 每个 rank 都有；rank 0 额外承载协调职责 |
| ModelWorker / NPU runtime | 执行本 rank 的模型计算与实际设备读写 | 提交成功与目标已经可读分开判断 |

**例子：A 有 4096 个 prompt token，所有槽都空闲。**

1. Main 预留额度，向 SR_P 发 alloc(A)。
2. 协调者选择 slot 0，并下发到 16 个分片。
3. 各分片占用本地槽，返回描述符。
4. 协调者收齐成功结果，才回 alloc_ack。
5. dispatch 后，各 scheduler 驱动共同的 prefill。
6. 各分片报告本 rank 的实际写回完成。
7. 若 rank 7 未完成，协调者继续等待。
8. 全部完成后，协调者才报告 prompt ready。

rank 0 自己也有分片，其结果同样需要汇总。
阶段 0 的 16 份主 KV 都覆盖完整逻辑内容。
这与 TP 模型计算如何分工是不同的维度。

现有 [scheduler.ingest_requests](../../python/sglang/srt/managers/scheduler.py:2097) 已调用 mempool_service.advance。
SR 将沿这个有序 tick 接入。
网络线程只收发并入队；allocator 和 TP 通信在约定的 tick 位置推进。
空闲 tick 也要处理控制消息，避免等待时没人推进。
tick 返回不表示 NPU 工作已经完成。

通信的拟定落地方式是 HTTP 加 ZMQ。
Main 对外收 token IDs，经 ZMQ 与协调者交换控制消息。
原生 HTTP 用于创建匹配 dispatch 的 Req，并保留输出路径。
直接访问后端 HTTP 也必须校验已获授权的绑定。
这些接口和模块名属于实施方案，尚未实现。

## C page、slot 和 NUMA 共享的准确关系

**阶段 0 已有逻辑 page 接口，alive 暂时由整槽承载。**
prefix 从 02 开始按 page 分配。
两者的分配粒度需要分别理解。

| 名称 | 阶段 0 的含义 | 示例 |
|---|---|---|
| DRAM 逻辑 page | B token；用于目录、页表和命中 | B=128 时 4096 token 为 32 页 |
| alive 存储 slot | 一个请求独占的连续区域 | P 每槽 8192 token，即 64 页额度 |
| HBM page | 原生 allocator 管理；本次验收为 128 token | 3968 token 占用 31 个完整页 |
| req_pool_idx | 原生请求行编号 | 与 P/D mempool slot 分别分配 |

P 池内逻辑页号为 `slot × (8192 / B) + 槽内页号`。
P slot 0 覆盖页 0–63；slot 1 覆盖页 64–127。
A 只在 slot 0 的页 0–63 写入有效 prompt。
页 32–63 仍被这个请求占用，不能借给别的请求。
因此账本扣除 64 页额度，不能只扣 32 页。

“最小空闲 slot”就是选择编号最小的空闲槽。
全空时选 0；0 占用而 1 空闲时选 1。
归还 slot 0 后，下一次可以再次选 0。
这个规则使槽位复用可以通过 alloc/free_ack 直接核对。

P、D 分别选择自己的最小空闲槽，编号允许不同。
D 的槽只放生成段主 KV。
若 prompt 长度为 4096，全局位置 4096 对应 D 槽内偏移 0。
全局位置 4097 对应偏移 1，须实际经过 forward 才有相应 KV。
刚采样但未经过 forward 的最后一个 token 尚无 KV。

**同一个 NUMA 节点与同一份物理 KV 分开判断。**

| 场景 | rank 0 的 page 7 | rank 1 的 page 7 |
|---|---|---|
| 阶段 0：两 rank 分配在同一 NUMA | 私有副本中的逻辑页 7 | 另一份私有副本中的逻辑页 7 |
| 后续共享接入：同一共享组 | 同一份物理主 KV 的页 7 | 映射这份相同物理内容，虚拟地址可不同 |

用户要求的“两 rank 共享 page 编号与内容”在后续共享接入实现。
该功能在原文称为 K1，安排在这四票验收之后。

| 数据种类 | 阶段 0 全功能 | 后续共享目标 |
|---|---|---|
| alive 主 KV | 每侧 16 份 | 每侧 8 份，每组 2 个 rank |
| P alive Index K DRAM | 16 份 | 1 份 |
| prefix 主 KV | 16 份 | 1 份 |
| prefix Index K | 16 份 | 1 份 |

01 尚未启用后三类 DRAM 区域，注册时必须如实标记。
D 生成 Index K 在阶段 0 留在 HBM。
共享以后仍可保留每 rank 一个 SR 分片。
分片数量与物理副本数由不同配置描述。

旧 [NUMA 共享 02](../ascend-mempool-numa-sharing/issues/02-mla-shared-kv-overlap.md) 只覆盖共享接入的部分范围。
它还需要适配新 Main/SR，并补齐其余数据共享与广播。
当前 [启动检查](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py:255) 仍拒绝生产 share=1。
共享组件实验通过不能替代真实模型服务接入。

page-first 布局安排在后续“远端 prefix 与布局调整”。
alive 真正的 page 分配器安排在“并发与动态容量”。
布局、分配粒度、物理共享是三项独立工作。

## D Ticket01：真实冷请求与显式释放复用

**完成标准：新 Main/SR 链真正驱动两次 NPU 推理，并正确归还和复用资源。**
本票不启用 prefix 或 P alive Index K DRAM。
已有 P 主 KV 写回、D 两侧读取、Index K 传输继续复用。
更细的拟定模块与协议见 [ticket01 实施方案](ticket-01-implementation-plan.html)。

**一次冷请求的路径**

1. 两侧注册实际布局、容量和 rank 集合。
2. Main 检查输入与资源，再关闭串行门。
3. Main 预留四类额度，SR 执行实际分配。
4. 两侧安装请求身份与 P/D 页表绑定。
5. 两侧 dispatch 确认后，创建原生 Req。
6. P 计算 prompt，并真实写回主 KV。
7. 原生通道传 Index K 和 handoff。
8. D 等三条件齐备，再输出首 token 并 decode。
9. D drain 与原生清理完成后，报告 finished。
10. Main 下发 free，两侧实际归还再回 free_ack。
11. 全部额度恢复后，才允许第二个请求进入。

| 预留资源 | 4096-token prompt、最多生成 128 时 |
|---|---|
| P alive | 1 个整槽；64 个逻辑页额度 |
| D alive | 1 个整槽；1 个逻辑页额度 |
| D Index K HBM | 每个必要 rank 实际预留 ceil(4224/128)=33 页 |
| D 原生请求行 | 每个必要 rank 实际预留 1 行 |

Main 管容量账本，SR 负责实际分配结果。
16 个副本的容量不能相加，算成 16 倍可服务请求数。
P 原生 HBM 由静态 prompt 上限保证，另随原生生命周期清理。

**01 最主要的三处实现工作**

| 工作 | 需要做到什么 | 需要防止什么 |
|---|---|---|
| D 原生预留 | alloc 时取走真实行和页；decode 从预留范围消费 | 账面减 33 页，后续才向全局池临时申请 |
| 全 TP 就绪 | Router ready、Index K、handoff 三条件全齐 | rank 7 迟到时，其余 rank 提前进入 decode |
| 显式释放 | drain、native 清理、finished、free_ack 分离 | 原有 DONE 或 drain 绕过 Main 直接归还 |

预留适配还要处理提前 EOS、实际尾页和未使用整页的归还。
已交给原生清理的页与尚未消费的页不能重复 free。
预留容量也不能伪装成已经写入 KV 的长度。

已核对的旧行为位于 [finish_drain](../../python/sglang/srt/disaggregation/ascend/mempool_control.py:500) 和 [_accept_done](../../python/sglang/srt/disaggregation/ascend/mempool_control.py:778)。
新模式要接管它们，以及早到、迟到 DONE 和取消回滚的分支。
继续保留身份、绑定和设备读写安全校验。

**三个完成事件各表示什么**

| 事件 | 可以据此做什么 | alive 是否可以复用 |
|---|---|---|
| P native release | P 原生 HBM 和请求行已清理 | 否；D 仍可能读 P alive |
| D finished | D 最后读写及原生清理已完成；退还对应原生额度 | 尚不能；还要走显式 free |
| SR free_ack | 对应 alive 已无依赖且实际归还 | 是；Main 才恢复整槽额度 |

01 的测试包含两个不同的 4096-token 冷请求。
完整输出与同长度的既有正式 mempool 冷算参考比较。
还要验证真实 decode、P/D 来源、Graph 和 padding 行。
CPU 侧覆盖迟到 rank、重复回执、拒绝、无 free 不复用等行为。
任何内部失败走 fail-stop，不在本轮强行回收后重试。
用户执行 NPU 验收并确认后，本票才关闭。

## E Ticket02：请求结束后保存 prompt 到 prefix

**完成标准：P 原生 HBM 和请求 alive 都释放后，prefix 仍保留完整数据。**
本票仍让请求冷算，关闭 match_prefix。
因此“保存正确”与“命中计算正确”可以分别验证。

本票增加三组能力：

| 能力 | 实现位置与职责 |
|---|---|
| P alive Index K | 每 rank 增加 21 层 × 16 槽 × 8192 token 的 DRAM 区域 |
| prefix 页池 | SR_P 按 B-token 页分配；每 rank 容量 40 页 |
| 目录与 admit | Main 管 token 页哈希、父关系、状态、源保护和 publish |

Index K 每 token 每 producer 层为 128 维 BF16，即 256 B。
主 KV 每 token 每层为 compact 576 维 BF16。
保存同时覆盖 78 层主 KV 和 21 个 Index K producer 层。
主 KV 与 Index K 都完成写回后，P 才能释放相关原生资源。
这样 D 结束后，admit 可以直接从 alive 取数。

**A 冷请求结束后保存 32 页的步骤**

1. SR_D 报告 finished；Main 重新检查目录。
2. 查出缺少 ready prefix 位置的 prompt 页。
3. 按实际空闲页数确定保存范围。
4. 先保护复制源和所需父路径。
5. 再解除请求对 alive 的 owner hold。
6. SR_P 分配 writing 目标，执行 admit 复制。
7. 全部必要 rank 确认目标可读，才回 ack。
8. Main 按父页先于子页的顺序 publish。
9. 解除源保护，满足条件后 free alive。

例如 A 的源页起初只有 owner hold，lock_ref=1。
计划 admit 后增加源保护，lock_ref=2。
解除 owner hold 后仍为 1，源不能复用。
复制完成后解除最后一份保护，才具备回收条件。
重复 ack 对这次操作只能生效一次。

ack 证明目标已经可读；设备提交返回不足以确认保存。
复制只能使用映射实际支持的 CPU 或 NPU 接口。
不把 BM 设备地址直接当作可解引用的 CPU 指针。
完成收据按请求和操作独立保留，不能依附已释放的原生请求行。

**容量、去重和失败行为**

| 情况 | 行为 |
|---|---|
| A：首次 4096 token | 保存 32 页，每份 prefix 占用 32/40 |
| B：A 加 1024 新 token，仍冷算 | 跳过已有 32 页，只增加 8 页，达到 40/40 |
| C：与 A 相同，仍冷算 | 已有 ready 内容全部跳过，仍为 40/40 |
| E：不同的 4096 token | prefix 已满，跳过保存，仍正常结束并释放 alive |
| 仅剩部分容量 | 从尾部缩短保存范围，保留可发布的连续前缀 |
| 复制失败或超时 | fail-stop；保留受保护源和 writing 目标，结束本轮 |

上述 A/B/C/E 是 02 的冷算保存变体。
它们不证明 03 所要求的命中长度或后缀计算。
02 的验收要在 alive 真正归还之后，再核对 prefix 内容。
每份 40 页是可缓存的逻辑容量，16 份副本不等于 640 页独立内容。

## F Ticket03：命中 prefix 后恢复与后缀计算

**完成标准：B 命中 4096，C 命中 3968，并产生各自正确的完整输出。**
两者都分配新的请求 alive，并扣除整槽额度。
prefix 来源页和当前请求 alive 是独立的存储位置。

命中后需要两份不同用途的数据：

| 操作 | 数据方向 | 后续使用者 |
|---|---|---|
| load_back，原文 op2 | prefix → P 原生 HBM | P 的后缀计算、完整 Index K 传输 |
| fill，原文 op1 | prefix → 本请求 P alive | D 的 prompt 主 KV 读取 |
| admit，原文 op3 | 本请求 P alive → prefix | 后续请求的命中 |

op1/op2/op3 是原文的操作名称，不能当作运行时固定 op_id。
每次真实操作仍由 Main 分配唯一、单调的 op_id。
执行顺序是先 load_back，再启动 fill 和后缀计算。
admit 在请求结束后执行。

```flow LR
匹配prefix -> load_back: 保护来源并恢复 HBM
load_back -> fill命中段: op2 全部完成
load_back -> 计算并写回后缀: op2 全部完成
fill命中段 -> prompt就绪: 命中段可读
计算并写回后缀 -> prompt就绪: 后缀可读
prompt就绪 -> D生成: 还须 Index K 与 handoff 就绪
```

fill 和后缀计算可以独立推进。
是否实际并行重叠，需要 NPU 测量证明。
Main 在计划时就分别保护两个操作的 prefix 来源。
load_back 与 fill 各完成一次，各解除一次自己的保护。

**B：4096 个相同 token，加 1024 个新 token。**

1. 匹配 A 保存的 32 页，hit_len=4096。
2. load_back 恢复这 4096 token 到原生 HBM。
3. 主 KV 按 token 映射拆回 512/64 两部分。
4. 21 个 producer 层的 Index K 另行恢复。
5. 构造长度为 4096 的 prefix_indices。
6. fill 写入 alive 的区间 [0,4096)。
7. P 只计算并写回区间 [4096,5120)。
8. 两段及所有必要副本完成，才报告 5120 ready。
9. D 满足三条件后，正常生成与释放。
10. 结束时只 admit 新增 8 页，prefix 达到 40 页。

prefix_indices 是逐 token 的原生 KV 索引。
它是逐 token 的 4096 个索引，逻辑和原生各对应 32 页。
若原生 allocator 返回不连续页，复制也必须按真实索引执行。

后缀跟踪器描述 [hit_len,prompt_len)，内部完成计数从零开始。
它不把 hit_len 伪装成已经完成的写回量。
若后缀先完成、fill 仍未完成，ready_tokens 仍不能越过空缺。
D 就绪取决于请求 alive，不等待结束后的 prefix admit。

**C：与 A 完全相同的 4096-token prompt。**

命中上限是 `floor((prompt_len - 1) / B) × B`。
因此 C 命中 3968 token，最后 B token（本例 128）仍由 P 重算。
该规则保留原生末段计算和首 token 采样路径。

| 内容 | C 的具体值 |
|---|---|
| DRAM 命中 | 31 页，即 token 0–3967 |
| HBM 有效内容 | 31 个完整页 |
| HBM 恢复 | 31 个页，每页 B=128 token；后缀另用第 32 页 |
| prefix_indices | 长度 3968，不包含后续 128 token |
| 重算范围 | token 3968–4095，写入第 32 页 |
| 结束时 admit | 最后一页的 prefix 已存在，跳过；总量仍为 40 |

“完全命中”表示整段 prompt 已有缓存来源。
本设计仍按上限保留最后一页计算。
DRAM与HBM页边界一致；旧半页接续要求已由统一页决定取代。

P 原生 HBM 在后缀写回和原生传输完成后可以释放。
它不等待独立读取 prefix 的 fill。
但 D 必须等待 fill 和后缀在 alive 中全部就绪。
B 的命中段结束后可先逻辑释放，整槽仍等待新增页 admit 完成。

03 同时验收复制内容、真实命中推理、实际尾页和生命周期。
B、C 各自与关闭 match_prefix 的冷算参考比较完整输出。
只看首 token 相同或最终文本相近不足以通过。

## G Ticket04：完整 demo 自运行验收

**完成标准：两台机器各执行一条命令，得到完整、可追溯的 V1–V10 结果。**
沿用并扩展 01–03 的进程管理、断言和报告入口。
最终集成版本从空 prefix 开始执行目标组。

| 用例 | prompt token 数 | hit_len | P 新算 token 数 | D Index K 预留页／rank | 结束时 prefix 页／份 |
|---|---|---|---|---|---|
| A | 4096 | 0 | 4096 | 33 | 32 |
| B：A 加新后缀 | 5120 | 4096 | 1024 | 41 | 40 |
| C：等于 A | 4096 | 3968 | 128 | 33 | 40 |
| E：不同内容 | 4096 | 0 | 4096 | 33 | 40 |

生成上限均为 128；D Index K 预留包含 prompt 加生成上限。
P alive 每次扣整槽 64 页额度，D alive 扣整槽 1 页额度。
prefix 满时 E 跳过 admit，并复用之前实际归还的槽。
fixture 按真实 token IDs 构造，记录每个用例的预期命中。

**入口自动完成的工作**

1. 核对两机配置、代码和模型身份。
2. 协调启动，等待实际注册与服务就绪。
3. 用关闭 match_prefix 的模式获取冷算参考。
4. 独立初始化目标目录和池。
5. 严格执行 A → B → C → E。
6. 运行拒绝用例并核对账本不变。
7. 在独立运行中注入错误和超时。
8. 汇集两侧日志，逐项生成验收结果。
9. 清理本次启动的进程；失败返回非零。

参考组和目标组不能相互污染缓存。
两组可依次运行，不要求同时加载两套模型。
01 使用既有正式 mempool 冷算作为新控制链的参考。
完整命中验收使用同部署关闭 match_prefix 的参考。
旧短请求日志不替代本次 4K/5K 长度的参考。

| 验收项 | 需要看到的证据 | 首次实现位置 |
|---|---|---|
| V1 命中 | B=4096、C=3968，以及来源页 | 03 |
| V2 内容 | 每 rank、每必要层、逐逻辑 token 比对 | 02 保存；03 恢复与 fill |
| V3 输出 | 各用例完整 output IDs 与自身参考一致 | 01 冷请求；03 命中 |
| V4 真实读取 | P/D 槽、偏移、内容与读取完成 | 01；03 扩展 |
| V5 P 提前释放 | P native 释放后，D 仍正确生成 | 01；02/03 扩展 |
| V6 账本 | alive 与 D 原生资源恢复；prefix=32/40/40/40 | 01–03 |
| V7 完整页边界接续 | 3968 个索引（31 页）、第 32 页重算、无重复保存；另测提前结束释放 | 03 |
| V8 入口拒绝 | 超长、未对齐、超生成上限、容量不足 | 01；04 组合复测 |
| V9 槽复用 | E 的 alloc 对应此前实际 free_ack 的槽 | 01；02/03 扩展 |
| V10 串行负载 | D 每步常驻和执行请求数均 ≤1 | 01 起持续检查 |

真实 P/D 取数需保留端到端与复制适配器证据。
适配器分别覆盖 P-only、D-only 和混合来源。
实际 sparse 选择可能使某层只从一侧读取。
因此不要求每层两侧都有正字节传输。

报告关联 run_id、版本、rid、op_id、rank、槽与设备完成事件。
事件先后要有完成和因果依据，不能只比较跨机裸时间戳。
缺 rank、缺设备完成、超时或账本不等式都不能算通过。
CPU、组件、smoke、完整验收分别标记。
日志与报告保存到 `/home/cryang/log/` 下的独立运行目录。

## H 实现后怎样启动，后续怎样核对进度

**以下是拟交付入口，不是当前可执行命令。**
已有详细启动提案见 [ticket01 方案 F 部分](ticket-01-implementation-plan.html)。
实现后以真实脚本的参数和帮助为准。

部署配置提供两机地址、权重路径、网卡、NUMA 节点、容量与日志目录。
两侧使用相同 run_id 和一致的配置摘要。
P 侧入口启动 Main Router 与 P 服务；D 侧入口启动 D 服务。
SR 随各 worker 自动加载，不需要另起 32 个 SR 服务。

P 机的拟定命令：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode serve \
  --config /home/cryang/mempool-min.json \
  --run-id stage0-serve-001
```

D 机的拟定命令：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode serve \
  --config /home/cryang/mempool-min.json \
  --run-id stage0-serve-001
```

两个终端提交启动命令，后续就绪检查由入口协调。
无需先等一侧模型完全 ready 才启动另一侧。
顺序为：环境检查 → Main 启动 → 两侧建池及模型初始化。
BM 映射就绪后，D 执行 Graph capture。
控制握手、完整资源注册通过后，Main 才接受请求。
映射就绪与控制就绪分开，避免互相等待。

常驻服务通过 Main 的 `/generate` 提交 token IDs。
`/health` 表示进程存活；`/ready` 表示两侧具备服务条件。
请求还须通过串行门，忙时按拟定方案拒绝，不增加等待队列。

自动验收使用相同入口，将两侧模式改为 `--mode acceptance`。
01、02、03 逐步扩展同一入口的验收范围。
完整 V1–V10 模式由 04 交付；此前不能报整阶段通过。
脚本负责请求、断言、收集和清理，不把这些留给手工协调。

阶段 0 启动保持私有副本配置，沿用连续 NUMA 分配。
共享接入需要后续实现，不能只把 share 开关改成 1。
所有设备复制、完成检查和 Graph 适配以 Ascend NPU 实际接口落地。
CPU 检查用于控制行为；硬件数据路径由用户执行 NPU 验收。

**回顾后的实现关注点**

| Ticket | 实施时最需要证明的事情 |
|---|---|
| 01 | 唯一分配来源；D 实际足额预留；全部旧自动释放分支被接管 |
| 02 | 主 KV 与 Index K 一起保存；源锁先建立；HBM 释放后仍能保存 |
| 03 | 原生 token 映射与实际尾页接续；fill 和后缀分别完成；完整输出一致 |
| 04 | 参考组隔离；完整 rank 与事件证据；错误运行不污染成功组 |

这次回顾未发现四票与阶段 0 核心合同的实质冲突。
但 HTTP/ZMQ 接线、预留适配和新增复制方向仍须实现验证。
这四票完成后，得到单请求功能验收，即原文的 L0。
随后承接共享存储接入，再进入远端 prefix 与布局调整。
这些后续目标不属于阶段 0 完成状态。

按 [AGENTS.md](../../AGENTS.md) 的临时规则，每次实现后立即更新目标页 D。
记录已实现、待实现、CPU 检查和用户 NPU 验收状态。
本轮只回顾文档与现有代码，未运行新功能测试或 NPU 验收。
四票保持 open，未修改验收勾选项。
