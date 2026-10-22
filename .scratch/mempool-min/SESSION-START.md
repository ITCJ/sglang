# mempool-min：新会话交接

日期：2026-10-22。用途：交接设计文档与当前代码基线，供下一会话用 askmatt 准备实施。

## 本轮任务

在 `/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang` 上，基于已确定的最小 demo 设计，用当前环境实际可用的 askmatt 工作流准备实现：核对代码与设计、形成实施规格、拆分可验收任务、提出第一个切片。先读文档和代码，再讨论真正影响实现范围的问题。

用户希望先让核心流程真实运行，框架能够逐步承载完整版。不要把完整版的并发、复杂容量不足处理、retract/resume、故障恢复全部提前放进最小版，也不要只搭 mock 框架。sparse 与 dense 两条轨道都已完成设计，部署时互斥；具体实现先后请结合依赖提出建议，不擅自把其中一条从目标中删除。

## 代码基线与工作区

- 当前分支：`cryang/dev/mempool-min`，已从 `cryang/dev/mempool` 的当前 HEAD `d328af12bd` 创建并切换。
- 分支由上述基点创建；随后用户要求提交并推送连续 NUMA 分配及全节点容量测试，以便服务器拉取。实际提交与同步状态以 `git log` / `git status` 为准。工作区另有原有已修改及未跟踪的设计文件，开始前检查相关 diff，不要将它们当作可以清理的临时文件。
- `d328af12bd` 实现的是 NUMA 配置与共享分组的纯规划层。现有服务仍主要是每侧 TP16、P_i/D_i 的独立池；生产 manager 仍拒绝 `share=1` 或显式 rank map 的组启动。不要把 SharingPlan、组件实验或文档里的共享设计当成真实服务已完成共享。
- 现有 sparse P/D 数据路径应复用：prompt 主 KV 留 P 侧 DRAM，生成主 KV 留 D 侧 DRAM，D 稀疏读取两处；原生 P/D 数据连接传 Index K 与 handoff 附属信息。main router、跨请求 prefix 复用及 dense 经 prefix 交接仍需实现。
- 先读仓库 `AGENTS.md`、`CONTEXT.md`、`docs/agents/issue-tracker.md`、`.scratch/ascend-mempool/verification.md`。旧票用于理解已实现能力与验收状态；新 demo 的目标以本交接和下述现行设计为准。

## syncfile：先读什么，每份文件做什么

文档根目录：

`/Users/hibikid/Documents/syncfile/sglang-kv-explainer`

这个目录是另一个设计/讲解 agent 的同步副本，**只读，不直接修改**。实施侧的新规格、拆票、问题与偏差记录写在当前仓库 `.scratch/mempool-min/`，每个实施 ticket 单独成文件。需要设计侧更新时，整理明确反馈交给用户。

| 阅读顺序 / 文件 | 作用与使用方式 |
|---|---|
| 1. `README.md` | 文件导航、最新同步日期、设计进展、哪些 HTML 尚未更新。先由它确定当前版本。 |
| 2. `mempool-min-demo/walkthrough.md` | **最小 demo 的现行流程和范围入口**。第 0–10 节是 sparse 最小版；第 11 节是 dense 最小版。包括部署、约束、模块、接口、数据生命周期、验收、后续阶段。先完整理解它，再读相关证据。 |
| 3. `mempool-min-demo/discussion.md` | 最小版的决策依据：已确认 M1–M53、实现事实、逐轮讨论、确认与被替代的候选。M1–M41 对应 sparse，M42–M53 对应 dense。查某条设计为什么这样定，以及其限制来自哪里。历史轮次保留旧说法，不代表仍有未决问题。 |
| 4. `mempool-min-demo/requirement.md` | 最小版原始需求与追加要求。解释必须保留的设计理念，以及为什么要减少复杂状态；不是将所有早期“候选”重新视为当前要求。 |
| 5. `mempool-design/walkthrough.md` | **完整版的目标架构**。第 0–6 节 sparse，第 7 节 dense。用于确认接口、资源语义和后续扩展方向，不能整体照搬为阶段 0 的工作量。 |
| 6. `mempool-design/discussion.md` | 完整版的 C1–C99、U1–U5、术语、正确性条件与讨论证据。C1–C85 是 sparse 及通用部分，C86–C99 加入 dense。按最小版引用查阅，重点读术语、当前已确认条目和 dense 第 31–34 轮。 |
| 7. `mempool-design/requirement.md` | 完整方案的需求原文和 dense 模式变更原文；用于追溯目标，不替代后续已确认决定。 |
| 8. `mempool-design/copy-perf-tests.md` | 传输性能实验清单及每项实验解决的问题。它是后续实测计划，不是已经测得的结果，也不全是最小版首个切片的前置条件。 |
| 辅助：`page/mempool-min-demo.html`、`page/mempool-full-demo.html` | 已制作的图解，帮助理解 sparse 流程。当前 README 明确说明 dense 的文字定稿尚未同步到 HTML；不能因为图里没有 dense 就遗漏它。文字与图不一致时按现行文字及用户最新决定处理。 |
| 背景：`page/sglang-kv-cache-explainer.html` | SGLang 主线的 radix/prefix cache、L1、HiCache、Mooncake、HiSparse 讲解，帮助理解原生机制；不是我们的服务实现规格。 |
| `mempool-html/plan-*.md`、`screenshots/`、`changelog/` | 绘图计划、页面检查与历史修改记录。实现时按需看，不是代码任务清单。 |
| `handover/handover-report.md` | 早期框架讲解页的接手报告，版本较旧；不是本次 mempool-min 的最新交接入口。 |

冲突处理：用户最新明确决定优先；最小版现行串讲稿定义本阶段范围，完整版定义长期目标；discussion 查最新已确认条目。不要让旧轮次、旧 HTML 或原始需求中的过时候选覆盖新决定。实测与“已经实现”的事实必须再对照当前代码和验收记录。

设计 agent 的源码证据来自 SGLang main `4fc86f73c2`。这是参考版本，和当前开发分支不是同一个版本；文件路径、行号、算子能力、设备接口都要重新定位核实。syncfile 使用通用传输/设备术语，落地以本仓库的实际后端为准，不能直接照搬参考环境中的 API。

## 尚未同步进设计文档、必须带入的新决定

### 1. dense 最小版更换首个模型

用户已经决定：最小版先不用 Kimi K3，两台机器放不下。改用官方模型 **`moonshotai/Kimi-Linear-48B-A3B-Instruct`**，先验证 MLA + KDA 的真实流程；增加机器后再换 K3。

syncfile 目前仍按 K3 写。保留其机制设计，但必须重新核实 Kimi-Linear 的 config、当前分支模型/attention backend、状态形状与 dtype、TP 支持、快照能力，以及加载所需容量。不要沿用 K3 的 24 个 MLA 层、69 个 KDA 层、429 MiB 状态、TP16 每 rank 27 MiB、256 页 864 MiB 等预算。

同样不要仅凭源码存在就声称双机能运行。P/D 相同 attention TP、prefix 放 P 侧、MLA 一份共享数据、KDA 按 rank 分片，以及 D 结束后的缓存保存都是已确认方向。TP16 是候选部署配置，需结合替代模型实际核实。用例的页边界逻辑可沿用，但容量和分读份额按实际参数计算。

### 2. NUMA 默认分配已改为连续

本轮用户已核对 SharingPlan 代码，只要求默认节点列表从轮转改为连续均分。当前工作区已实现：按各主机内 rank 升序连续分配到有序节点列表，余数优先给前面的节点。规划器和 `MempoolKVManager.create()` 共用算法；显式映射仍可指定其他分组。

例如 4 ranks 配 `[0,2]` 得到 `[0,0,2,2]`；16 ranks 配 8 个有效节点时每节点两人。实际节点 ID 必须来自机器拓扑，不要把示例中的偶数编号写死。

本轮 48 项 CPU 检查、Ruff lint/格式检查通过。CPU 报告：`/tmp/sglang-numa-contiguous-20261022/report.json`。没有执行本次修改后的 NPU 建池、映射和 probe 检查，相关硬件结果待补。

### 3. 最新机器反馈与测试安排

用户 2026-10-22 最新反馈：刚检查到 NUMA 对 HCCS/Fabric 的覆盖已经完整，按用户表述“2T 都可以分配 HCCS”。将“2T”按用户原话记录；本会话没有收到此次原始地址报告或全容量分配日志。

因此，早期“只有偶数 NUMA 与窗口相交、总交集约 680 GiB”的快照不能继续当作当前机器约束。后续按最新拓扑和报告配置全部可用 NUMA。地址覆盖、实际空闲/可分配容量、共享读写正确性与性能是不同证据，不能把本次反馈写成新共享服务或满容量读写已验收。

NPU 机器预计几天内不能使用，硬件测试后补。现在可以推进读代码、规格、拆票、CPU 可验证部分和测试入口准备；硬件依赖项明确列出。按仓库流程保留硬件验收门槛，不用 CPU 通过代替关闭硬件验收项。

后续测试安排（2026-10-23）：用户指定P `10.120.72.23`、D `10.120.72.25`，NUMA 0–7，每节点96 GiB。新增 `run_bm_startup_gate.sh --all-numa`：16个rank连续分配，每rank48 GiB、每机768 GiB；只检查创建、映射、probe、共同持池和正常释放。命令见 `ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md`，报告在 `/home/cryang/log/`。新增检查器是 `scripts/check_bm_numa.py`；原 `check_bm_even_numa.py` 保留兼容入口。该硬件测试尚未执行。

全节点配置加入后，CPU gate 51项通过；本次改动与检查记录见同目录 `numa-startup-verification.md`。

## 核心设计提醒

- main router 管请求调度、页哈希目录、容量与操作编排；SR 管具体存储分配和传输；推理进程管计算、HBM 页与恢复。地址由拥有映射的 worker 解析，router 不持有可直接解引用的设备指针。
- sparse 最小版复用当前 P/D 路径：alive 与 prefix 不去重；命中数据既要恢复 P 的 HBM，也要进入请求自己的 alive；P 只写新算段。prompt 默认留 P，生成段留 D。Index K 的缓存附属区域、页/槽的临时后端、旧控制路径的分配/释放接管，按最小串讲稿落实。
- dense 没有 alive：P 命中 prefix 后恢复到 HBM，完成计算并 save 到 prefix；全部相关 save 完成后汇总 `saved`，router 才为 D reserve/dispatch；D 从 prefix 恢复，decode 在 HBM 中运行，结束后保存新增整页及对应状态。
- dense 的 MLA 页是一份共享数据，多 rank 分读后机内 all_gather；KDA 是按 rank 分片的递归状态与短卷积窗口，各 rank 恢复自己的片。不可套用 sparse 阶段 0 的“每 rank 一份 prefix”；实际共享访问能力是 dense 的必要实现项，当前 SharingPlan 本身不提供它。
- 首 token 由 P 采样，放进 `saved` 到 router 暂存，再随 `dispatch` 交给 D；D 完成恢复后通过统一输出路径发送。router 不提前发送，D 不重新采样首 token。
- dense 页大小、状态检查点粒度、D 跟踪间隔统一用注册的 HBM page_size，当前验收设计为 128；模型内核 chunk 大小是另一回事。最小版关闭 overlap，P、D 每请求均为一个工作槽加一个跟踪槽。
- KDA 命中需要对应边界状态，只有 MLA KV 不够。P 保存命中上限边界和 prompt 末尾交接状态；D 结束保存最后一个有效整页边界状态。工作槽/跟踪槽与 prefix 里的记录槽分开记账。
- 完全命中仍受 `floor((prompt_len - 1) / page_size) * page_size` 限制，并受可恢复状态边界限制。最后一个采样 token 尚未经过 forward，因此没有自己的 KV。
- 最小版用串行、单 chunk、受限输入、足额 reserve、简单入口检查和 fail-stop 减少分支。正常路径的 ack、源保护、真实完成、释放与容量归还不能省略。dense 的 D 资源在 saved 后预留；prefix 的必需交接容量在 P 开始前保证。
- P 在 saved 发出后可以释放 HBM，不依赖 D 的 load_back；正常流程不强制两者先后。VD3 通过测试门控先确认 P 释放，再允许 D 恢复，以证明交接数据独立存活。
- sparse 验收 V1–V10 与 dense 验收 VD1–VD10 分开。dense 固定长度测试为 greedy、ignore_eos、max_new_tokens=129、清除额外 stop 条件，检查原始输出 IDs 恰好 129 且按 length 结束；F 按 token IDs 构造，验证 decode 新增缓存可复用。测试入口失败返回非零，硬件日志统一放 `/home/cryang/log/`，双机允许每机一条命令并自动协调。

## 建议的代码入口

- `python/sglang/srt/hardware_backend/npu/mempool/`：现有布局、manager、控制协议、读写、生命周期与 sharing plan。
- `ascend-mempool-test/`：独立组件测试、CPU gate、自运行双机入口和历史硬件记录。
- `.scratch/ascend-mempool/`、`.scratch/ascend-mempool-numa-sharing/`：已完成和未完成能力、规格、分票与用户验收记录。`ticket-02-1-summary.md` 是 d328 的历史交付，轮转规则已被本轮修改替代。
- 当前分支的 scheduler、P/D disaggregation、原生 prefix cache、`kimi_linear` 模型与 KDA/MLA backend：根据实际调用关系搜索。不要直接使用 syncfile 主线证据中的行号判断本分支行为。

## 下一会话第一轮应交付

1. 用简洁中文确认理解的最小范围和两种模式差异，标出本交接覆盖的旧数字/旧约束。
2. 结合代码形成“已有 / 最小版新增 / 后续阶段 / 待硬件验证”的差距表；共享计划与实际共享、模型代码存在与已验证运行分别表达。
3. 按 askmatt 的实际流程准备本仓库实施规格与任务依赖，提出可独立审阅、CPU 开发可推进、之后有明确硬件验收的首个切片。不要仅按源码文件机械拆票。
4. 提出 sparse/dense 的实施顺序建议，以及 Kimi-Linear 参数重算清单。仅对确实影响最小范围或重大返工的 2–3 个问题和用户讨论，其余沿用已确认设计。
5. 暂不更新 syncfile 或 HTML，不重新开展整轮架构设计。原始需求中“当时只讨论、不写代码”是设计阶段记录；本次已进入实施准备，后续按当前用户指令和 askmatt 流程推进。
