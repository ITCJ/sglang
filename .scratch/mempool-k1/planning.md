---
title: K1 规划：先补单请求性能基线
subtitle: 阶段 0 的测量准备、完整共享范围与实施切片草案
template: doc
theme: blueprint
lang: zh
date: 2026-10-27
status: 范围与推进顺序已确认；规格、正式票据与实现尚未交付
---

**先补阶段 0 的单请求性能基线，再实施 K1 完整共享。**
K1 的目标副本数为 `8 / 1 / 1 / 1`。
并行优化与正式性能门槛留给后续独立工作。

## A 已确认的范围与依据

用户在本轮确认“阶段0已经结束”，要求开始 K1 规划。
随后明确选择以下顺序：

1. K1 先验收完整共享功能，性能优化后续单独推进。
2. 先补阶段 0 的单 request 基线，再实施 K1。

测量准备不要求先把阶段 0 调优到某个性能目标。
阶段 0 的功能验收与性能基线分别记录。
本稿只记录规划；代码检查基点为 `0ee7a299bc` 加当前工作区。

阶段 0 已有 P alive Index K、prefix 主 KV 和 prefix Index K。
后续切片在这些功能上增量修改，每个交付版本保持保存与命中可用。
此前将第 2 票写成关闭 prefix 的中间态不合适，已修正。

| 依据 | 用途 |
|---|---|
| [syncfile 现行 walkthrough](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/walkthrough.md) 第 0、1、8.3 节 | K1 的四类副本、NUMA、广播与阶段边界 |
| [阶段 0 完整验收](../mempool-min/verification/ticket04-002-20261027/README.md) | 已通过的功能与生命周期基线 |
| [目标对齐页](../mempool-min/target-alignment.md) C、D | 阶段 0 与 K1 的范围对应 |
| [既有共享规格](../ascend-mempool-numa-sharing/spec.md)及[ADR 0001](../../docs/adr/0001-mla-shared-kv-publication.md) | 复用共享组件与安全发布合同 |
| [输出验证约定](../mempool-min/output-verification-policy.md) | 标准答案、精确 KV、完整 IDs 差异分别验证 |
| [阶段交付规则](../ascend-mempool/verification.md) | 用户运行 NPU，验收后关闭对应票据 |

旧共享 02 同时含功能接入与并行优化，不能直接等同于 K1。
本轮将其功能合同带入新控制路径，性能部分后置。
旧票状态和原始验收证据保留；正式拆票时补充承接关系。

## B 之前测过什么

**尚无正式的阶段 0 单请求性能基线。**

2026-10-26 的[手动 smoke 记录](../mempool-min/verification/glm52-mempool-demo-20261026/user-reported-response.json)包含：

| 项目 | 已有证据 |
|---|---|
| 请求 | 128 个输入 token，48 个实际输出 token |
| prefix | `cached_tokens=0`，冷请求 |
| 后端计时 | `e2e_latency=11.120827391045168` 秒 |
| 采样 | 单次用户回传；没有重复采样或分位统计 |
| 缺失 | TTFT、逐 token 间隔、Main 完整耗时、阶段耗时 |

这条记录属于自然语言功能 smoke。
不能用输出 token 数除以该耗时推导稳态 decode 速度。
早期 BM 组件带宽结果也不代表完整模型服务性能。
04 的本地归档是功能验收摘要，未提供单请求性能报告。

## C 单请求基线的测量草案

复用现有双机自运行入口和 Main `/generate` 路径。
固定 GLM-5.2 权重、量化、TP16、Graph、NUMA 和容量配置。
保持真实并发为 1；每个请求等全部 `free_ack` 后再开始下一个。

| 场景 | 输入与实际命中 | 需要回答的问题 |
|---|---|---|
| 冷请求 | 4096 token，命中 0；另留 128-token 短输入参考 | 没有复用时的首 token、生成及收尾成本 |
| 部分命中 | 5120 token，命中前 4096 token | 恢复与后缀计算的时间 |
| 完全命中 | 4096 token，实际复用 3968 token | 恢复后重算最后一页的时间 |

完全命中沿用阶段 0 的最后一页重算规则。
长生成用实际 128 输出 token 的独立负载；自然 EOS 短输出单列。
短输出不能记为 128-token decode 覆盖。

**计时必须注明观察点。**
现有 Main 只支持非流式请求，返回前还要完成 admit 和 free。
客户端单次 HTTP 总耗时包含这段收尾。
现有 `meta_info.e2e_latency` 来自 D 后端，计时边界不同。

| 指标 | 计划口径 |
|---|---|
| 后端 TTFT | D 请求进入至首次输出可用，标明后端观察点 |
| ITL | 同一观察点相邻输出 token 的时间差；保存原始样本 |
| TPOT | 首次输出至结束的平均后续 token 时间，单独报告 |
| 后端生成耗时 | 保存 D 返回的 `e2e_latency` 与实际输出长度 |
| Main 完整耗时 | 客户端发出请求至完整响应接收完成 |
| Main 收尾 | 后端结果到齐至保存、释放结束，使用 Main 本地时钟 |

当前非流式内部默认每 50 token 刷新一次输出。
已有 `SGLANG_FORCE_STREAM_INTERVAL=1` 可用于首 token 测量。
`--enable-metrics` 可返回 `first_token_latency` 等字段。
后端 TTFT 复用这套观察点。

现有 ITL 指标只保留有限桶、count 和 sum。
原始逐 token 样本需新增可关闭的 Ascend 采样。
建议读取已有 prebuilt/decode 完成时间，记录 CPU 结果就绪间隔。
这类样本与 tokenizer 观察点分别标注，不冒充客户端流式 ITL。

实施时核对首次输出、批次粒度及样本数量。
有限桶直方图不能替代原始 ITL 样本；缺项明确标为缺失。

每组比较使用相同采样配置，并记录采样带来的 IPC 条件。
各进程使用本地单调时钟计算区间，跨机时间戳不直接相减。

测量运行关闭 `verify_prefix` 的内容回读及故障注入。
内容正确性另跑既有验收；正式计时与 profiler 采集分开。
优先复用现有观测点；必要的新计时放在 Ascend/NPU 边界。
设备提交时间不能标作设备完成时间，避免为计时逐步强制同步。

预热和重复轮次由入口配置，报告保存每次原始结果。
每轮明确准备 prefix；核对实际命中和目录状态后计入相应场景。
冷、部分命中、完全命中分别统计，不能混为一个延迟分布。

记录实际代码、工作区差异、模型身份、输入和输出 IDs。
双机每机一条命令，自动完成准备、就绪、请求、报告及自身进程清理。
日志保存在 `/home/cryang/log/`，失败或缺少必需证据时返回非零。

**本项完成条件：** 可重复的单请求报告，经用户运行并核对采样口径。
当前没有新性能数字，也没有已交付的基线 CLI。

## D K1 完整共享要交付什么

| 数据 | 阶段 0 副本 | K1 默认副本 | 必须验证的行为 |
|---|---|---|---|
| P/D alive 主 KV | 每侧 16 | 每侧 8 | 每组 2 rank 共享物理内容，分工写、完整读 |
| P alive Index K | 16 | 1 | 指定 writer 保存；保存结束前保护来源 |
| prefix 主 KV | 16 | 1 | 只保存一份，fill 覆盖必要 alive 副本 |
| prefix Index K | 16 | 1 | 只保存一份，恢复到各 rank 的原生 HBM |

单份 prefix 的 `load_back` 采用 rank 0 读取后广播。
三类单份区域的副本数独立于 alive 主 KV 的共享组数。
8 个 alive 主 KV 副本不会各自附带一份 Index K 和 prefix。
P alive Index K、prefix 主 KV、prefix Index K 在 P 侧各只有一份。
原生 HBM Index K 继续满足各 rank 的计算和 P→D 交接需要。
D 生成的 Index K 仍只留 HBM，不增加 DRAM 区域。

组成员、owner、writer 和 reader 分开表达。
四类副本数分别注册、分别算容量和描述符。
SR 分片仍对应所有 worker，不随副本数减为 8 或 1。

NUMA 先分配，再按配置生成共享组。
默认每节点两个 rank，显式映射使用全部目标 NUMA 节点。
实现支持规划器已有的可变组大小，不能写死相邻双 rank。
请求 NUMA、物理贡献和已验证物理页落点分别报告。

基础同步属于 K1 正确性：本步 KV 本地供给、历史 KV 完整发布。
所有 readers/writers 完成后才能释放和复用。
保留 ADR 0001 的安全发布原则；采用保守同层完成方案作为起点。
selected 并行、跨层提前写回及 3% 性能门槛留给后续性能工作。

K1 继续沿用串行、单 chunk、layer-first 和 alive 整槽后端。
远端 prefix、page-first、真实并发和 alive 页分配各属后续阶段。

## E 建议的实施切片

以下为可审阅草案，尚未发布正式 issue。
每个实现切片都包含 CPU 检查、对应 NPU 自运行入口及用户验收。
上游通过对应硬件验收后，再进入依赖它的实现。

| 顺序 | 交付行为 | 直接前置 |
|---|---|---|
| 1. 阶段 0 单请求基线 | 冷、部分命中、完全命中的计时报告及可重复入口 | 已有阶段 0 功能基线 |
| 2. alive 主 KV 共享，保留 prefix 功能 | 达到 `8/16/16/16`；共享读写、Graph、释放与复用，已有保存和命中继续可用 | 1 |
| 3. Index K 与 prefix 单份存储 | 达到 `8/1/1/1`；单份保存、发布与源保护，各 rank 从共享源恢复并完成 B/C | 2 |
| 4. prefix 单次读取与广播 | rank 0 读取后广播，各 rank 恢复；继续验证 fill、后缀计算与生成 | 3 |
| 5. K1 完整自运行验收 | ABCE、共享内容与贡献、Graph、拒绝、超时、长生成及双端清理 | 4 |

```flow LR
阶段0单请求基线 -> alive主KV共享: 保留prefix功能
alive主KV共享 -> Index与prefix单份存储: 保持保存和命中
Index与prefix单份存储 -> prefix单读广播: 完成读取方式切换
prefix单读广播 -> K1完整验收: 组合验证
```

各票使用完整五票列表的编号，基线计为第 1 票。
实施中间态的 DRAM 副本数明确如下：

| 切片 | alive 主 KV／侧 | P alive Index K | prefix 主 KV | prefix Index K |
|---|---|---|---|---|
| 1. 阶段 0 基线，启用 prefix | 16 | 16 | 16 | 16 |
| 2. alive 主 KV 共享，保留 prefix 功能 | 8 | 16 | 16 | 16 |
| 3. Index K 与 prefix 单份存储 | 8 | 1 | 1 | 1 |
| 4、5. 单读广播与完整验收 | 8 | 1 | 1 | 1 |

第 2 票保留阶段 0 的三类私有区域及原生 HBM Index K 交接。
prefix 保存、load_back 和命中恢复继续运行。
fill 写入共享 alive 时按组分工，避免多个 rank 重复写同一目标。
admit 从对应共享 alive 副本保存到各 rank 的私有 prefix。
本票验收同时覆盖冷请求与 B/C 命中，不能只测关闭 prefix 的配置。

第 3 票把三类区域从各 16 份改为各 1 份。
所有需要读取的 rank 都取得这份物理数据的有效映射。
过渡读取方式为各 rank 读取同一份 prefix，恢复各自的原生 HBM。
保存和命中在本票均须通过；单份源不能由隐藏的私有 DRAM 副本替代。

第 4 票把该读取方式改为 rank 0 读一次再广播。
这使广播切片独立可验收，同时保持此前的 prefix 功能。

第 3 票达到四类存储副本目标，第 4 票完成 K1 的单读广播要求。
切片 5 复用已有 full-acceptance，不把前几片的正确性检查推迟到最后。

最高验证边界沿用完整 Main/SR/P/D 服务。
CPU 使用现有服务与 TP tick 边界；硬件覆盖实际内容、映射和完成条件。
K1 完成后再用相同测量口径对照阶段 0，性能调优另行立项。

## F 代码核对结果与接续入口

这是当前代码的缺口索引，不是已实施文件清单。

| 建议阅读顺序 | 用途与缺口 |
|---|---|
| [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | 已有分组规划；启动仍拒绝共享，group_layout 未带入三类附属区域 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 实际建池和映射仍为 P_i/D_i 配对，需要承接多成员生命周期 |
| [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | 当前 replicas 固定 16，需要四类实际副本身份 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | prefix locations、容量和回执仍按 16 worker 的私有副本表达 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 注册需上报区域贡献；Main 响应与后端计时边界在这里连接 |
| [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 描述符读取需从本 rank 私有区域扩展到对应共享区域 |
| [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | load_back、fill、admit 需改为单份源及规定分工 |
| [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 分开本 rank 写入量与整个副本的可读范围 |

现有入口：[run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py)。
其 share 配置目前固定为 0，尚无性能基线模式。
复用既有多成员 BM 组件与关闭经验，服务接入仍须独立验证。
功能改动优先落在 Ascend/NPU 目录；其他改动逐项说明必要性。

当前阶段按 `grill-with-docs → to-spec → to-tickets` 推进。
下一步核对上述服务验证边界与切片，再形成可实施规格与正式票据。
本轮完成只读核查和规划记录；没有功能改动或新的 CPU/NPU 测试。
