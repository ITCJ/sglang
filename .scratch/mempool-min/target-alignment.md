---
title: mempool-min：实现目标对齐核对
subtitle: 以 syncfile 现行文字为依据，区分阶段 0 与后续目标
template: doc
theme: blueprint
lang: zh
date: 2026-10-24
status: 统一页修正及CPU检查完成；用户NPU验收待执行
---

**当前四票与 sparse 阶段 0 一致，但没有覆盖后续全部目标。**
alive 整槽分配和私有副本都有阶段 0 的明确依据。
K1 共享、真正的 alive 页分配及 dense 集成仍须承接。
此前说明没有充分区分阶段终点与整体目标。

## A 核对依据与结论

用户本轮指定 syncfile 内的 mempool-min-demo 为最终实现目标。
现行依据是 [walkthrough.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/walkthrough.md)。
第 0–10 节定义 sparse 流程及后续阶段，第 11 节定义 dense 轨道。
[discussion.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/discussion.md) 用于追溯 M1–M54。
现行正文与图解按2026-10-24的M54/C100统一页决定同步。
历史候选及历史图解快照不覆盖现行文字。

| 核对项 | 现行设计 | 本地规格与方案 | 判断 |
|---|---|---|---|
| Main Router / SR | 独立 Main；rank 0 协调；每 worker 一个分片 | 相同；网络线程只入队，tick 内通信 | 阶段 0 一致 |
| 统一 token 页 | sparse/dense、DRAM/HBM、P/D与相关池严格共用B | 当前GLM-5.2集成仅放行128；注册核对实际各组件 | 已修正；NPU待验证 |
| page 与 slot | alive 用临时整槽后端；prefix 按页分配 | 相同；整槽扣账、free_ack 后归还 | 阶段 0 一致 |
| 四类副本数 | 阶段 0 为 16 / 16 / 16 / 16 | 相同；ticket01 尚未启用的区域如实注册 | 阶段 0 一致 |
| 就绪与释放 | 三条件启动 D；finished 与 free_ack 分开 | 相同；接管旧分配、绑定与自动释放 | 阶段 0 一致 |
| prefix 与验收 | 保存、恢复、后缀；A/B/C/E、V1–V10 | 分布在 02、03、04 | 阶段 0 一致 |
| K1 与后续阶段 | 阶段 0 / L0 → K1 → 阶段 1 | 四票之后缺完整承接清单 | 任务覆盖缺口 |
| dense 轨道 | 单独的共享 prefix 与状态交接流程 | 保留目标，尚未拆本仓库集成票 | 后续覆盖缺口 |

这是文档范围核对，不代表这些设计已经实现。
2026-10-24统一页决定取代此前的64/128分离；本轮已修正现行正文和ticket01代码。
HTTP/ZMQ及原生预留已有实现与CPU证据；硬件验证仍待用户执行。

## B 最小空闲 slot 0 与 page 的关系

依据：walkthrough 第 0、3、5 节，M17、M31、M40。
阶段 0 的 DRAM 逻辑页与原生 HBM 页统一为可配置的 B token，当前部署 B=128。
alive 暂时按整槽分配，页表描述其中的有效数据。

以 S_P = 8192、prompt = 4096 为例：

| 项 | 数值与含义 |
|---|---|
| 一个 P 存储槽 | 8192 / B = 64 个逻辑页额度 |
| slot 0 | 对应池内逻辑页 0–63 |
| 本次有效数据 | 使用 slot 0 的前 32 页，即页 0–31 |
| 剩余尾部 | 页 32–63 仍被本请求占住，不能再次分配 |
| 释放 | 最后一项依赖完成后归还整槽，收到 free_ack 才恢复额度 |

“最小空闲槽”表示从空闲槽中选编号最小者。
全空时选 0；若 0 被占用且 1 空闲，则选 1。
这个规则用于明确验证归还后的复用。
它既不是“最小空闲页”，也不是原生 req_pool_idx。

```text
阶段 0 alive：逻辑页接口 → 连续存储槽临时后端
阶段 0 prefix：逻辑页接口 → 真正的页分配器
阶段 1：调整为 page-first 布局
阶段 3：alive 替换成真正的页分配器，读路径支持散页
```

page-first 改变数据排列；页分配器改变分配和回收单位。
两项工作分别安排在阶段 1 和阶段 3。
原生 HBM 与 DRAM 注册同一个 B；只满足整数倍关系仍须拒绝。

## C 两个 rank 共用 NUMA 与共用一份 KV

依据：walkthrough 第 0、3、8.3 节，M12、M20、M37。
NUMA 分配决定请求在哪个节点分配内存。
共享决定两个 rank 是否映射同一份物理数据。
只把 rank 0、rank 1 放到 NUMA 0，不会自动合并副本。

| 示例 | 阶段 0 私有副本 | K1 共享目标 |
|---|---|---|
| rank 0 和 rank 1 | 各持有一份完整主 KV | 同属共享组，持有同一份主 KV |
| 逻辑 page 7 | 页号相同，背后是两份物理内容 | 组内同一逻辑页对应同一份物理内容 |
| 写入 | 两个 rank 各完整写自己那份 | 按配置分工写入，汇总后供所有 reader 读取 |
| 地址 | 各进程解析自己的映射 | 共享物理内容，进程虚拟地址仍可不同 |
| TP16、每 NUMA 两个 rank | 每侧 16 份 alive 主 KV | 每侧 8 份 alive 主 KV |

K1 的四类副本数必须分别处理：

| 数据类型 | 阶段 0 | K1 |
|---|---|---|
| alive 主 KV，R_am | 16 | 8；默认每组 2 个 rank |
| P alive Index K，R_ai | 16 | 1 |
| prefix 主 KV，R_pm | 16 | 1 |
| prefix Index K，R_pi | 16 | 1 |

D 生成的 Index K 在这两个阶段仍只留在 HBM。
K1 还包括单份 prefix 的 load_back 读取与广播。
SR 分片数量由 worker 数量决定，不等于物理副本数。
共享后分片解析组映射；Router 仍只处理逻辑位置和不透明描述符。

## D 名称、验收与实施票怎样对应

**2026-10-25 ticket01 原生 warmup 适配，第 4 步：本机验证完成。**
共享测试 helper 整理后，完整 CPU 套件再次 421/421 通过。
Standards 和 Spec 均无剩余发现；静态检查及四组入口描述通过。
公共 HTTP 文件与重试补丁前逐字节一致。

本次独立提交接在 ticket01 后，不包含 ticket02 实现。
用户在 31/32 用新 run-id 重跑硬件验收；01 仍为 open。
02 旧提交已撤回，待 01 验收通过后重写。

**2026-10-25 ticket01 原生 warmup 适配，第 3 步：CPU 首跑与 review。**
完整 CPU 套件 421/421 通过，4 个功能文件 strict mypy 通过。
13 个改动 Python 文件通过仓库 lint、格式、isort 与 AST 检查。
Spec review 无发现；Standards 指出重复测试 loader，已共用并拆分 fixture。
共享测试 helper 修改后重新验证，NPU 尚未执行。

**2026-10-25 ticket01 原生 warmup 适配，第 2 步：定向 CPU 回归通过。**
真实 HTTP 回归分别验证 P/D：先等监听，再发本机 fake warmup，最后 freeze_gc。
本机回环测试明确绕过代理；公共 HTTP 实现无需连接重试。

原生 D 预分配、7 步 decode 和原生释放归还页及请求行。
同一池随后能预留真实请求；未预留真实请求和混批均被拒绝。
16-rank service warmup 后仍完成真实请求和显式释放。
完整 CPU 套件、双轴 review 和用户 NPU 验收仍待执行。

**2026-10-25 ticket01 原生 warmup 适配，第 1 步：最小路径改动已完成。**
fake warmup 先于 SR dispatch 检查识别，沿用原生分配与释放。
D 的三处既有 hook 仅对真实请求执行预留逻辑。
真实请求缺少预留仍失败，fake 与真实请求混批也失败。

参考组和目标组均启用 server warmup，并等待两端 ready。
fake batch 不计入真实 decode 步证据，不建立 alive 绑定。
公共 http_server.py 已恢复重试补丁之前的内容。
已复现 dispatch 误拒绝和 D 预留 KeyError；修复后回归待运行。

**2026-10-25 ticket01 原生 warmup 适配，第 0 步：撤回未验收的 ticket02。**
用户要求先稳定 ticket01，再重写 ticket02。
当前分支已移除旧 ticket02 提交及实现，保留恢复引用。
其他未提交修改保持原样；01 与 02 均为 open。

本次仅适配各机的原生 fake warmup，不跨 P/D 传请求数据。
临时 HBM 和请求行由原生 scheduler 分配与释放。
真实请求仍须经过 Main/SR；拟撤回公共 HTTP 重试修改。
本步仅完成代码基线整理，回归和 NPU 验收均待执行。

**2026-10-25 ticket01 第二次 NPU 验收：HTTP 启动竞态修复。**
31/32 在 93678e5fd4 通过身份检查，进入参考服务请求阶段。
P 日志显示 4096-token Prefill、DONE 和 free=16。
验收扫描到启动时的 freeze_gc ConnectionRefused，尚未进入目标组。

跳过 warmup 也跳过了监听等待，启动线程提前调用 freeze_gc。
真实本地 HTTP 回归已复现；现增加有时限的连接重试。
HTTP/TLS 错误和持续连接失败仍记录异常，验收器保持原有检查。

本次需修改公共 HTTP 启动函数，NPU hook 无法控制监听顺序。
本地 HTTP 回归 6 项、参考验收器 9 项通过，静态检查通过。
双轴 review 无阻断项；已补连接耗时计入预算的测试。
修复单独交付在 ticket01 之后；整票仍等待用户 NPU 验收。

**2026-10-25 ticket01 首次 NPU 验收：身份比较修复。**
31/32 已建立连接，复制与模型测试尚未启动。
本地 numa_nodes 为 tuple，经 JSON 传输变为 list，导致相同配置被误拒绝。

配置现先转成 JSON 表示；失败时也保存两侧身份和逐字段差异。
验收入口 CPU 10 项、静态检查通过；Standards / Spec 均无发现。
修复单独交付在 ticket01 基线上，等待用户在原目录重跑；01 保持 open。

**2026-10-24 统一页修正，第 1 步：实现已修改，验证待运行。**
Main 配置、页哈希、入口对齐、整槽账本和 SR 页描述统一从 `page_size=B` 推导。
NPU mempool 记录实际 B；注册前核对 worker、DRAM、allocator、KV 池和 Index K。
当前 GLM-5.2 mempool-min 配置默认并仅允许 128；不是所有 Ascend 后端的能力声明。

**第 2 步：已调整现有 CPU fixture 和自运行 NPU 入口。**
验收检查每 rank 的 DRAM/HBM 注册、整槽页表、额度与 native 预留。
启动从 PAGE_SIZE 传入统一 B；提前 EOS 输入按 B 对齐。

**第 3 步：已补充页大小负向和容量 CPU 测试。**
覆盖配置支持范围、P/D 任一 rank 不一致、实际 worker 组件不一致。
覆盖哈希分块参数、4096/5120 页数、整槽页表和验收报告拒绝旧口径。

**第 4 步：本地 spec、方案、目标页、回顾及 tickets 01–04 正文已同步。**
讨论与交接记录已追加新决定，旧 Comments 保留。

**第 5 步：完整 CPU 套件 416/416 通过（2026-10-24）。**
统一页负向配置、组件传递、容量、哈希参数、预留和释放检查通过。

**第 6 步：现行图解已重生成，原交互图保留为历史快照。**
小例和正式验收保持原 token 容量；代码已格式化。

**第 7 步：7 文件 strict mypy、13 文件 lint/AST 和启动 dry-run 已通过。**
现行决策索引和残留示例已复核；完整 CPU 416 项通过。

**第 8 步：图解公式检查及逐文件交付记录已完成。**
两套现行图解各通过6组公式、7种不支持B和容量对齐检查。
完整CPU日志与静态检查记录已归档，见[本轮改动与验证清单](unified-page-size-change.html)。

**第 9 步：syncfile设计源17个文件已写回并逐文件校验。**
完整设计、最小demo、讨论与交接、现行图解及历史快照均已同步。
dense检查点与跟踪间隔使用B，kernel chunk和缓存分块单独校验。
本轮实施与本地验证完成；剩余为用户NPU验收。

统一页修正按用户要求并入原ticket01提交；当前提交号以git log为准。

ticket01 保持 open；NPU 未执行，02–04 不解锁。
此前408项通过属于修正前记录；本轮完整CPU为416项。


依据：walkthrough 第 8 节，M19、M22。
原稿同时使用功能阶段和负载验收两套编号。
共享接入又单独记为 K1，放在负载与容量轨道中。
把它们直接串成路线图，会混淆开发内容和验收目标。

| 原文标记 | 表达什么 | 与实现的关系 |
|---|---|---|
| 阶段 0–4 | 功能逐步增加什么 | 基础请求流程、远端 prefix、流水、并发等 |
| L0–L3 | 验证哪种负载和容量 | 从单请求功能验收到长上下文并发验收 |
| K1 | 共享存储接入这项功能 | 安排在基础请求流程验收后、远端 prefix 前 |
| ticket01–04 | 当前发布的具体实施任务 | 合起来实现并验收基础请求流程 |

**L0 是阶段 0 的功能验收，不是另一个开发阶段。**
K1 是原文的共享接入标记，不代表“阶段 1”。
后续说明优先使用中文功能名，括号内保留原编号供回查。

已经明确的主推进顺序为：

```flow LR
基础请求流程并验收 -> 共享存储接入: 功能验收后接入共享
共享存储接入 -> 远端prefix与布局调整: 继续扩展
```

上述三项分别对应原阶段 0（含 L0 验收）、K1、阶段 1。
基础请求流程包括冷请求、prefix 保存与命中、生成、释放和复用。

| 功能名称（原文标记） | 当前本地承接 | 状态与边界 |
|---|---|---|
| 基础请求流程（阶段 0） | mempool-min 01 → 02 → 03 → 04 | 已按确认回顾重编四票；待实现与验收 |
| 共享存储接入（K1） | 旧 NUMA 共享 02 有部分相关范围 | 尚无覆盖完整共享接入的新控制路径集成票 |
| 远端 prefix 与布局调整（阶段 1） | 本 feature 尚未拆票 | 包含 page-first |
| 分块与流水（阶段 2） | 本 feature 尚未拆票 | 包含逐 chunk 推进、提前首 token |
| 并发与动态容量（阶段 3） | 本 feature 尚未拆票 | 含 alive 页分配；并发与动态容量分步推进 |
| 生成缓存复用等扩展（阶段 4） | 本 feature 尚未拆票 | 包含 decode admit、混合页及副本重配 |
| dense 最小版 | 同事推进 baseline；本仓库集成未拆票 | baseline 交付仍需核对 |

**阶段 0 四票进度（2026-10-23 重新拆票）**

用户已确认详细回顾，并要求按该内容重新拆票。
四票已原位重编，保留编号、历史记录和直接依赖。

ticket01 代码和测试入口已交付，等待用户 NPU 验收；四票均保持 open。
现有 NPU 数据路径与共享组件证据不计为这四票完成。
逐票职责、数据流、验收与启动安排见[四票详细回顾](stage-0-ticket-review.html)。
执行索引见[ticket-plan.md](ticket-plan.md)，正式验收清单以独立票为准。

| Ticket | 已完成 | 待实现的主要行为 | 验证状态与进入条件 |
|---|---|---|---|
| [01 冷请求与显式释放](issues/01-cold-request-lifecycle.md) | 冷请求及统一页代码已交付；身份比较已修复，启动改为原生 fake warmup | 用户更新独立 warmup 适配提交，在 31/32 使用新 run-id 重跑 NPU 验收 | 第二次运行在参考组日志检查发现 freeze_gc 启动异常；原生 warmup 适配 CPU 421 项、静态检查及双轴 review 通过；目标组 NPU 验收待执行 |
| [02 prompt 保存](issues/02-prompt-prefix-save.md) | 规格、回顾确认；旧实现已撤回 | 01 稳定后重写 P alive Index K、prefix 页池、admit 和 publish | 未通过 NPU 验收；等待 01 关闭后重写 |
| [03 命中恢复与后缀](issues/03-prefix-restore-and-suffix.md) | 规格、回顾确认及重编实施票 | load_back、fill、两段就绪与实际尾页接续 | CPU／NPU 新行为未验收；等待 02 关闭 |
| [04 完整自运行验收](issues/04-full-demo-acceptance.md) | 规格、回顾确认及重编实施票 | 独立参考组、A/B/C/E、V1–V10 与报告整合 | 完整验收未执行；等待 03 关闭 |

每次实现后立即更新本表，分别记录代码、CPU 检查与用户 NPU 验收。
只有用户确认对应 NPU 验收后，才关闭该票并解锁下游。

负载验收单独列出，不插入功能阶段编号：

| 验收名称（原文标记） | 要证明什么 | 主要依赖 |
|---|---|---|
| 单请求功能验收（L0） | A/B/C/E、V1–V10；生成上限 128 | 基础请求流程 |
| 32K × 16 验收（L1） | 逐步增至真实并发 16，再验证 32K 输入 | 实际并发能力；不依赖动态容量 |
| 64K × 16 验收（L2） | 16 个真实请求、64K 输入 | 共享、实际并发及容量验证 |
| 128K × 16 验收（L3） | 16 个真实请求、128K 输入 | 共享、实际并发及容量验证 |

后三项尚未拆本 feature 的验收票，也未通过相应硬件验收。

旧票是 [ascend-mempool-numa-sharing/02：MLA 共享 KV 与写入并行](../ascend-mempool-numa-sharing/issues/02-mla-shared-kv-overlap.md)。
它与 mempool-min/02 的 prefix 保存是不同的票。
旧共享票覆盖共享主 KV、写入并行和真实服务证据。
其中 02-1 已实现配置规划，真实共享服务部分仍为 open。
生产 [启动检查](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py:255) 仍拒绝 share=1。
共享建池组件通过不代表模型服务已接通共享。

旧票保留 rank-pair 控制；新阶段 0 改由 Main/SR 接管。
后续 K1 必须适配新的分配和释放合同。
还须补齐 P alive Index K、prefix 的单份存储及广播。
因此，不能把旧共享 02 的范围直接等同于整个 K1。
其性能票 03 也不能替代这些功能接入工作。

中文名称用于说明已有范围，没有新增一套阶段编号。
后三类功能的具体依赖仍需拆票，不由上表行序推断。
D2 / D3 仍按原文保留为完整初版之后的可选扩展。

## E 本轮校正与保留的边界

本地规格、拆票索引和 ticket04 已明确标成 sparse 阶段 0。
它们的完成只证明 L0 和 V1–V10，不证明 K1 或后续目标完成。
ticket01 及 SR 讲解补充了私有副本和整槽后端的阶段限定。
后续目标及当前承接缺口集中记录在本页。

当前四票的功能与验收边界继续按阶段 0 执行。
K1 不是阶段 0 的前置条件；它也是明确的后续必接能力。
本轮没有发布新的实施票，也没有关闭任何票。

两项既有用户决定继续作为对 syncfile 的明确补充：

- NUMA 按当前拓扑连续均分；早期部分节点限制不再写死。
- dense 首模型改为 Kimi-Linear；容量须按实际模型重算。

dense 的 baseline 分工不会取消共享 prefix 与状态交接目标。
其范围与模型调整见 [实施讨论](discussion.md) 和 [会话交接](SESSION-START.md)。

## F 检查范围与证据

本轮对照了 syncfile 现行串讲稿、决策索引及本地规格。
同时核对了四张票、ticket01 方案、SR 讲解和旧共享票。
本次沿Main、SR、实际worker、DRAM配置、native预留和Index K传输调用链核对统一B。
CPU及静态检查证据见[本轮改动与验证清单](unified-page-size-change.html)。
文档和代码检查不能替代 NPU 验收。

实施参照：[阶段 0 规格](spec.md)、[拆票索引](ticket-plan.md)、
[ticket01 方案](ticket-01-implementation-plan.md)、
[SR 与 Scheduler](sr-scheduler-explained.md)。

本次按用户授权同步 syncfile 设计源、现行 HTML 与当前仓库。
