# GLM-5.2 双机 sparse mempool 最小 demo：阶段 0

**Status:** ready-for-agent

**State:** open

日期：2026-10-22。范围：当前仓库的 sparse 实施主线。
`ready-for-agent` 表示规格可以进入任务拆分，不表示功能已经实现或通过验收。
本轮发布规格；任务拆分在后续 to-tickets 阶段单独审阅。

**目标对齐（2026-10-23）：** 总体实现目标以 syncfile 的
[mempool-min-demo/walkthrough.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/walkthrough.md)
现行文字及用户明确补充为准。本规格与 01–04 只覆盖 sparse 阶段 0 / L0。
K1 共享、后续功能阶段和 dense 轨道继续保留；阶段 0 完成不代表整体目标完成。
目标对应关系、旧共享票的覆盖限制见[目标对齐核对](target-alignment.md)。

## Problem Statement

现有 Ascend mempool 路径已支持将 prompt 主 KV 保留在 P 侧 DRAM，将生成主 KV
保留在 D 侧 DRAM，并让 D 从两处取数；原生 P/D 连接负责 Index K 和 handoff。
但是，请求的分配、绑定、就绪和释放仍由旧控制路径推进，尚无统一的 Main Router、
SR 接口及跨请求 prefix 保存与恢复，无法运行已确认的 sparse 最小 demo。

需要先用真实模型和真实 mempool 建立可验收的正常请求闭环，再加入 prefix 复用。
最小版应保留正确的数据所有权、完成确认和容量归还语义，使后续扩展有稳定边界；
本阶段通过串行、单 chunk、足额预留和受限输入控制工作量。

## Solution

实现双机 GLM-5.2 sparse mode：P、D 各一台 16 卡机器，各运行 TP16、attention DP1
的推理实例。新增独立 Main Router 和推理进程内的 SR_P／SR_D，统一管理请求、目录、
资源预留与操作编排。复用现有 P/D 主 KV 读写、原生 Index K 传输和 handoff 能力。

阶段 0 目标包括 P 侧 prefix 保存、部分命中和完全命中恢复、后缀计算、D 正确生成、
prefix 满时跳过保存，以及请求资源实际释放后的复用。alive 与 prefix 独立持有数据；
本阶段各 rank 使用自己的副本，prefix 放在 P 机。

用户已确认首个切片先完成：Main Router → SR → 真实 P/D 冷请求 → finished →
实际 free_ack → 第二个冷请求复用已归还槽位。prefix 保存与命中在后续切片接入，
首片完成不等于完整 sparse demo 完成。

dense 仍是后续目标。Kimi-Linear baseline 相关实现由同事同步推进，当前 sparse
实施和验收不依赖其完成；本规格不包含 dense 集成。

## User Stories

1. 作为 demo 使用者，我希望通过统一入口提交 token IDs，以便在固定模型配置下复现实验。
2. 作为 demo 使用者，我希望先跑通真实双机冷请求，以便确认新调度链确实驱动现有 P/D 数据路径。
3. 作为运维者，我希望服务注册实际布局、容量和 NUMA 分配，以便在接收请求前发现不兼容配置。
4. 作为 demo 使用者，我希望超长、未对齐或不支持的输入在分配前被拒绝，以便失败请求不占用资源。
5. 作为运维者，我希望每个请求一次预留必要资源，以便正常 decode 不进入动态扩容或等待流程。
6. 作为维护者，我希望 Main Router 统一编排、SR 负责实际分配，以便系统只有一套有效的槽位所有权。
7. 作为维护者，我希望页描述符由持有映射的 worker 解析，以便目录与调度逻辑不依赖设备地址。
8. 作为维护者，我希望所有 TP ranks 一致推进，以便个别 rank 的迟到不会造成提前计算或释放。
9. 作为 demo 使用者，我希望完成的 prompt 可以保留在 prefix 池，以便后续请求复用相同前缀。
10. 作为 demo 使用者，我希望部分命中后只计算后缀，以便验证 prefix 恢复确实减少了重复计算。
11. 作为 demo 使用者，我希望完全相同的 prompt 按既定上限命中并重算最后一页，以便仍能正确采样首 token。
12. 作为维护者，我希望恢复过程同时处理主 KV 和 Index K，以便 indexer 与 attention 使用一致的历史内容。
13. 作为维护者，我希望 DRAM 逻辑页与原生 HBM 页统一用 B，以便目录、恢复和原生预留共享相同边界。
14. 作为维护者，我希望命中段填充和后缀写回分别证明完成，以便 ready_tokens 不把尚未写入的数据当作可读。
15. 作为 demo 使用者，我希望首 token 和 decode 等到完整就绪，以便不会读到不完整的 prompt 或 handoff。
16. 作为运维者，我希望 P 原生 HBM 可以先于 D 结束归还，以便确认后续读取依赖的是保留的 DRAM 数据。
17. 作为维护者，我希望 finished 与 free_ack 各自具有明确语义，以便计算结束不导致仍在使用的源数据被复用。
18. 作为维护者，我希望复制源一直受到保护直到目标可读，以便异步完成或迟到 ack 不破坏数据生命周期。
19. 作为运维者，我希望按整槽记录 alive 占用，以便未使用的槽尾不会被重复分配。
20. 作为 demo 使用者，我希望 prefix 已存在时不重复保存，以便重复请求不扩大缓存占用。
21. 作为 demo 使用者，我希望 prefix 满时缩短或跳过保存，以便正常请求无需依赖淘汰或等待协议。
22. 作为运维者，我希望下一个请求复用实际归还的最小编号空闲槽，以便可直接验证释放和重新绑定。
23. 作为维护者，我希望重复 ack 不重复解锁或归还额度，以便消息重复不会破坏账本。
24. 作为运维者，我希望错误或超时使本轮 fail-stop，以便未完成操作的内存不会被继续分配。
25. 作为验收者，我希望比较每个用例的完整输出 token 序列，以便避免只检查首 token 或可读文本而遗漏差异。
26. 作为验收者，我希望在真实设备上核对复制内容和 P／D 来源，以便确认模型实际使用了 mempool 数据。
27. 作为验收者，我希望每台机器一条命令即可自动完成测试和收集报告，以便减少手工协调与遗漏。
28. 作为维护者，我希望报告区分 CPU、组件、smoke 与完整验收，以便未完成的硬件验证不会被误报为已交付。

## Implementation Decisions

### 1. 部署、布局与输入范围

| 项目 | 本阶段约定 |
|---|---|
| 模型与并行 | GLM-5.2；P、D 各 TP16，attention DP1，沿用当前支持的 PP1／普通非投机路径 |
| 主 KV | 78 个独立层，每 token 每层 compact 576 维 BF16，即 1152 B；共享 top-k 不代表共享层 KV |
| Index K | 21 个 producer 层，每 token 每层 128 维 BF16，即 256 B；无独立 scale 缓冲 |
| token 页 | DRAM／原生 HBM 同一个可配置 page_size=B；目录、命中、alive／prefix／Index K 共用 |
| 当前后端范围 | GLM-5.2 mempool-min 默认且仅接受 B=128；逐 rank 校验真实 allocator、KV 池和传输配置。512／64 是主 KV 特征维 |
| alive 后端 | 每 rank 每侧保留 16 个固定存储槽，layer-first；S_P、S_D 分别配置且为 B 的倍数 |
| 四类副本数 | R_am、R_ai、R_pm、R_pi 分开注册；sparse 阶段 0 均为 16，同侧共享组大小为 1 |
| prefix 后端 | P 机每 rank 一份，layer-first，真正按逻辑页分配，包含主 KV 和 Index K |
| 输入 | 只接受 token IDs、greedy；不支持 logprob、LoRA、多模态；extra_key／cache_salt 为空但仍计入哈希种子 |
| 执行 | 同时最多一个真实请求、单 chunk；原生 P/D radix cache 关闭；sparse decode admit 关闭 |

上表描述 sparse 阶段 0 目标。首片尚未接入 P alive Index K 和 prefix 时，注册应如实
标记这些能力未启用，不得将尚未分配的资源计为可用；后续切片再启用相应区域和副本。

prompt_len 必须为 B 的倍数，且 B ≤ prompt_len ≤ P_max。
P_max 取 S_P、单 chunk 上限和可容纳整个 prompt 原生 HBM 的静态上限的最小值，
包含恢复回来的命中段。生成上限满足 1 ≤ max_new_tokens ≤ min(4096, S_D)。

继续使用当前 NUMA 连续均分规则：每机 rank 按升序分配到有序节点列表，余数优先
分给前面的节点；实际节点来自机器拓扑。注册报告请求的分配，不据此宣称物理页落点
已验证。当前生产路径仍使用 P_i／D_i 配对的独立池；SharingPlan 只代表规划能力。

### 2. 角色与接口边界

Main Router 是独立 Python 进程，负责 token 前端与页哈希、目录、匹配、入口检查、
资源预留、操作编号、源保护、publish、结束后的 admit、串行门和 fail-stop。
本阶段请求输入已经是 token IDs，不增加文本接口的验收要求。

SR_P／SR_D 是推理进程内模块。各侧 tp_rank 0 的 scheduler 承担协调者，每个 worker
包含一个分片。协调者负责本侧分配与各 rank 完成汇总；分片持有映射并执行实际操作。
SR_P 同时管理 P alive 和 prefix；SR_D 管理 D alive，并向推理路径提供 P／D 页表。
网络接收线程只入队，TP collective 在有序 scheduler tick 中执行。

对外使用以下行为接口，具体传输和类型在实施时贴合现有代码：

- register：上报身份、布局、并行度、每类副本数、NUMA 分配、容量与静态输入上限。
- alloc／dispatch：统一预留后由 SR 分配，返回页表并向 P／D 分发请求与绑定。
- load_back／fill／admit：带 rid、op_id、源与目标描述符；ack 表示目标已经可读。
- ready_tokens／ready：SR_P 汇总后报告 Main Router，再经 SR_D 门控 D。
- finished：D 全部相关工作和原生资源清理完成；free／free_ack：明确下发回收并确认实际归还。
- error：携带请求或操作身份及原因，触发本轮 fail-stop。

沿用 bootstrap room 配对和必要的身份、布局、边界校验；旧控制协议的自主槽位
分配、绑定和自动释放副作用由新流程接管。不能同时保留两套独立的槽位分配事实。
原生 P/D 数据连接继续传 Index K、handoff 和目标页索引信息。

### 3. 注册、预留与物理分配

register 必须覆盖统一 page_size=B（包括实际 DRAM／HBM，严格相等，整数倍不算一致）、槽数与 S_P／S_D、prefix 页数、四类副本数、
D HBM Index K 页容量、D 原生请求槽位数、P 的静态 prompt 上限及 rank → NUMA 分配。
身份或布局不兼容时不进入可接收请求状态。

route 前先检查输入与容量；不满足约束时返回拒绝，账本和目录不变。通过检查后，
一次 reserve：P alive 一个整槽、D alive 一个整槽、D HBM Index K 的
ceil((prompt_len + max_new_tokens) / B) 页，以及 D 原生请求槽位一个。
P 原生 HBM 的适配由静态上限约束，P 原生请求行随 P 原生资源清理，不加入这四项账本。

SR 协调者统一选择本池编号最小的空闲槽，将一致结果下发本侧各 rank。
P、D 槽号可以不同。alive 的逻辑页接口由整个连续槽暂时承载；一个请求即使只有
少量有效页，也占用 S_P／B 或 S_D／B 页的整槽额度，槽尾不可再分配。
同槽最后一项依赖完成并收到实际 free_ack 后，才恢复整槽容量。

账本按机器、attention group 和资源种类分别记录；prefix 独立按页记账。
每次资源转换核对总量与未归还预留，不能把逻辑释放或解除 hold 记作物理容量归还。

### 4. alive 与 prefix 数据

P prompt 主 KV、D 生成主 KV 沿用现有 mempool 读写。D 使用位置与 prompt_len 区分
两侧来源，D 槽内位置为全局 token 位置减 prompt_len；prompt 不占用 D 槽的生成容量。

为完整 prefix 功能新增 P alive 的 Index K DRAM 附属区域，几何为每 rank
21 层 × 16 槽 × S_P token × 256 B。P 后缀的主 KV 和 Index K 都须真实写回完成。
D 生成段的 Index K 保留在 HBM，本阶段不增加其 DRAM 写回。

prefix 与 alive 独立分配、独立计费；即使命中 prefix，也为本请求分配完整的 alive
区域并填入命中段。每个分片只复制自己的那份数据，Router 不假设能直接访问映射。
只有合法可用的 CPU 映射才可使用 CPU 复制，否则由实际持有映射的 worker 使用
适合该设备映射的复制接口。

### 5. 目录、命中与源保护

复用已有 token 页哈希与命名空间种子语义。目录记录父哈希、子节点数和位置列表；
位置以机器、池、逻辑页号标识，附带 writing／ready／draining／freed 状态与 lock_ref。
同页在不同池中是不同位置；每类副本的描述符列表对 Router 不透明。

match_prefix 只使用连续 ready 的有效来源，优先 prefix。命中上限为
floor((prompt_len - 1) / B) × B token，保持按 DRAM 页对齐。P 不保留原生 HBM
前缀缓存；即使 prompt 完全相同，也重算最后一个 B-token 逻辑页。

lock_ref 表达 owner hold 与在途读操作的保护。计划操作时就建立源保护，不能等
网络发送或设备提交后再补锁；目标可读的 ack 到达后，每个 op 只解除一次对应保护。
op_id 在本轮内单调分配，重复 ack 不重复 publish、解锁或归还。
保留现有请求身份校验，不扩展通用重试、换源、代号或多轮恢复状态机。

### 6. 命中恢复、写回与 D 就绪

命中请求先完成 op2 load_back：按原生 allocator 分配 HBM 与逐 token 槽位映射，
将 compact 576 维拆回 512／64 两部分，另行恢复 Index K。所有必要 rank 的目标
可读后才确认 op2。构造的 prefix_indices 是逐 token 的 KV 槽位索引，长度等于
hit_len，不能用逻辑页号列表代替。

op2 完成后，op1 将 prefix 命中段填入请求自己的 P alive；P 同时可以计算后缀。
两者写入范围不重叠，是否实际重叠执行由设备证据判断。后缀 [hit_len, prompt_len)
独立跟踪写回，内部计数从零开始并校验连续性；不能预填旧计数器来伪装命中段已写。

ready_tokens 只统计请求自身 alive 中从零开始连续完成的内容。命中段来自 op1，
新算段来自真实写回；主 KV 与 Index K 的所有必要副本完成后才能计入。
它不等待请求结束后的 prefix admit。新 alive 位置完成后才 publish。

P 使用恢复与新算的完整 Index K 进行原生传输。D 的首 token 输出与 decode 必须
同时满足：Main Router 确认 prompt 全部 ready、所有相关 rank 的 Index K 传输完成、
所有相关 rank 的 handoff 到达。整个 TP 组一致推进，P 采样的首 token 按既有 handoff
传递，不因收到部分就绪信息提前输出。

### 7. 结束、保存与释放

P 本请求原生 HBM／请求行在后缀主 KV 与 Index K 写回完成且原生发送完成后释放。
命中时不等待 op1，因为 op1 独立读取 prefix；释放后 alive 及其完成收据仍须有效。
首片尚无 P alive Index K 附属区域时，保留既有主 KV 写回与原生传输完成条件；
新增 Index K DRAM 写回必须与 cold prefix 保存一起接入并纳入释放条件。

D 停止提交本请求新工作，等待全部相关 rank 的最后读写结束，清理 Index K HBM
与原生请求行，再由 SR_D 汇总 finished。Router 此时可归还已实际清理的 D 原生资源
额度，alive 仍等待独立 free 流程。D drain、P 收到 DONE 或迟到 DONE 的旧自动归还
路径在新模式中均不能直接释放槽，旧 RELEASE_ACK 不直接等同于新 free_ack。

收到 finished 后，Router 对 prompt 做 admit：重新检查目录，只保存缺少 ready
prefix 位置的页；已有页跳过。先为复制源和所需父路径建立保护，再解除 owner hold。
prefix 容量不足时从尾部缩短保存范围或跳过，不执行淘汰。

op3 将必要页复制到 prefix，目标可读后 ack，并按父页先于子页的顺序 publish。
不需保存的 alive 页可先逻辑释放；整个槽仍等最后依赖解除。Router 下发 free，
SR 实际归还后汇总 free_ack，Router 才恢复对应整槽额度。
下一请求只在上一请求的 admit 完成且全部 free_ack 收齐后进入 route。

### 8. 错误处理与现有能力边界

任何操作失败、超时、推理错误或账本不等式都终止本轮 demo。Router 停止下发新操作
和 free，记录现场并等待已发操作的完成或失败；超时由测试入口结束其启动的进程。
不在本轮内强制回收后重试，不复用可能仍被读取或写入的内存。

复用既有映射初始化、设备完成检查、native 清理和 Graph 数据路径。新控制接线仍须
获得对应硬件证据，旧短请求或组件通过不代表新 4K／5K 请求已验收。
当前私有副本切片不引入同侧共享 writer 的优化；已确认的共享发布 ADR 保留给后续
共享阶段，不把其完整性能任务链作为本规格的前置。

## Testing Decisions

### 已确认的测试边界

2026-10-22，用户确认以下边界。以真实服务端到端行为作为功能验收主入口：

| 边界 | 验证内容 | 证据限制 |
|---|---|---|
| Main Router → 真实双机 P/D | 输出、就绪、实际数据来源、结束、释放、复用、完整 prefix 用例 | 必须使用真实模型和 NPU；假 SR 不能替代 |
| CPU service／TP tick | 注册与入口拒绝、分配一致性、迟到／重复 ack、就绪门控、源保护、账本与 free 行为 | 替换传输、设备完成与 collective 边界；不证明远端可见性或 Graph |
| NPU 复制适配器 | prefix fill／load_back 的逻辑内容，P-only／D-only／混合取数、实际尾页与原生映射 | 补充服务证据，不单独判定 demo 完成 |

优先扩展现有 service／TP tick 的行为测试和已用过的双机 GLM-5.2 自动验收框架。
复用其进程管理、就绪、原始 token 保存、模型身份核对及日志汇总经验；旧 checker
里的自动释放条件需要按本规格调整。测试观察公共操作的结果、完成事件和资源账本，
不围绕新增私有方法逐个断言调用顺序。

### 首个冷请求切片的验收

1. Main Router 与两侧 SR 注册后，经真实 P/D 顺序完成两个内容不同、各 4096 token
   的冷请求，生成上限 128；使用 S_P = 8192、S_D = 128，本切片不依赖 prefix 保存或命中。
2. 记录完整输出 token IDs，与同输入、同模型、同配置的既有正式 mempool 冷算路径
   作参考对照。参考须在本次目标长度运行，不能用归档短请求代替。验证实际 decode
   forward 和 mempool 读取，不能只生成 P 的首 token 就判定闭环完成。
3. 主 KV 真实留在 P／D 两侧，Index K 与 handoff 使用保留的原生通道；所有相关 rank
   的就绪条件收齐后才开始 D。P 原生资源可以提前释放且后续输出仍正确。
4. finished 只出现在最后读写及 D 原生清理之后；alive 容量只在显式 free 后的实际
   free_ack 归还。延迟一个 rank 的完成不得提前恢复额度，重复 ack 不得重复归还。
5. 第二请求获得前一请求实际归还的最小编号空闲槽。P／D 分别核对自己的槽位绑定，
   不能只凭串行顺序推断复用。拒绝输入不改变目录或账本。
6. 每步记录 D 的真实常驻和执行请求数，均不超过 1。P 沿用 eager 路径；D 的真实
   Graph capture／replay 及 padding 行安全性沿既有路径验收，eager 运行可用于定位。
7. CPU 行为检查、必要组件检查与双机自运行入口就绪后交付用户；用户完成对应 NPU
   验收并确认后才关闭该切片。报告明确它只覆盖冷请求，不宣称 V1–V10 全部通过。

### sparse 阶段 0 完整验收的固定配置与用例

S_P = 8192、S_D = 128；每份 prefix 40 个逻辑页（容量保持 5120 token）；原生 HBM page_size = 128。
P_max、context、单 chunk 与 HBM 配置必须容纳 5120-token prompt、128-token
生成上限及原生必要余量，不能以缩短用例代替容量验证。

每逻辑页主 KV 为 B×78×1152 字节（10.96875 MiB），Index K 为 B×21×256 字节（0.65625 MiB）；40 页合计每份
465 MiB，16 份约 7.27 GiB。这是 prefix payload，实际分配还要计对齐、probe、
运行时及其他池的容量；不是整机内存需求或硬件分配通过证明。

| 用例 | prompt | 预期 hit_len | D Index K 预留页数 | 请求结束后 prefix 页数／份 |
|---|---|---|---|---|
| A | 冷请求 4096 token | 0 | 33 | 32 |
| B | A 的 4096 token + 1024 新 token | 4096 | 41 | 40 |
| C | 与 A 相同 | 3968 | 33 | 40 |
| E | 不同内容的冷请求 4096 token | 0 | 33 | 40，满容量跳过 admit |

四例均为 greedy、生成上限 128，fixture 保存实际 input IDs、sampling 参数和模型
身份。使用同一部署关闭 match_prefix，为 B、C、E 分别生成自己的冷算参考。
参考组与目标组各自初始化目录和池，目标组从空 prefix 开始，严格按 A → B → C → E
运行；参考请求产生的缓存不能污染目标组的命中与容量断言。
开启现有确定性选项并核实 DSA／自定义 kernel 的实际覆盖；该选项本身不证明确定性。

### V1–V10 验收条目

| 编号 | 必须证明的行为 |
|---|---|
| V1 命中 | B 命中 4096，C 命中 3968；route 日志能追溯对应 prefix 来源 |
| V2 复制 | 每 rank、每逻辑 token、每必要层核对 fill 后 alive 和 load_back 后 HBM／Index K；按真实 token 映射比较 512／64 两部分，不对布局不同的整块内存直接比较校验和 |
| V3 输出 | B、C 与各自冷算参考的完整输出序列一致；优先逐 token 比较。若必须使用数值容差，须由该用例自己的重复冷算校准并说明覆盖限制，不能沿用其他用例的阈值或只比较首 token |
| V4 真实来源 | 真实服务证明读取对应 P／D 槽、偏移、内容及生命周期；组件分别覆盖 P-only、D-only、混合来源，不要求每层两侧都复制正字节数，不使用将 P KV 清零作为负向用例 |
| V5 P 提前释放 | 证明 P 原生 HBM 在 D finished 前释放，且之后的生成仍与参考一致；保留完成与因果证据，跨机裸时间戳不单独充当顺序证明 |
| V6 账本 | 每次请求最终恢复 P／D alive 槽、D Index K HBM 与原生请求槽位；P 每请求扣整槽 64 页额度，D 扣整槽 1 页额度；prefix 为 32 → 40 → 40 → 40。B 的命中段可先逻辑释放，整槽归还必须晚于新增页 admit 完成 |
| V7 完整页边界接续 | C 的 prefix_indices 长度 3968，恢复 31 个完整页；最后 128 token 在第 32 页重算。admit 不重复保存，prefix 仍为 40 页；另测提前 EOS、未用预留与实际尾页清理 |
| V8 拒绝 | prompt 超过 P_max、不按 B 对齐、生成上限超过 128、注册容量不足时明确拒绝，目录与账本不变；其他不支持的输入功能同样在入口拒绝 |
| V9 释放复用 | E 分到 A–C 已使用并实际归还的 P／D 最小空闲槽，输出与自身冷算参考一致；各 free_ack 晚于对应最后读写与依赖完成 |
| V10 串行 | 全程 D 每步真实常驻与执行请求数均 ≤ 1；Graph batch width 或客户端并发参数不能替代该证据 |

任一非预期差异、缺失 rank、过早就绪／释放、账本错误或超时均失败。首片、组件、
smoke、完整 V1–V10 的覆盖分别报告，未执行项明确保留。

### 自运行入口与硬件门槛

入口自动完成配置与身份核对、启动、服务健康检查与注册就绪、参考和目标请求、
断言、日志及报告收集、失败处理与自身进程清理。双机允许每机一条命令，后续步骤
自动协调；失败返回非零，日志和报告统一保存到 `/home/cryang/log/` 下的独立运行目录。

报告包含代码版本及必要的工作区差异标识、模型／tokenizer／量化配置、部署参数、
实际输入与输出 IDs、rank 覆盖、设备完成、资源账本和失败位置。事件至少能够关联
rid、op_id、rank、位置／槽号、ready、ack、publish、finished 与 free_ack。
耗时可用于诊断，本规格不设置吞吐提升或共享优化的性能达标门槛。

实际 NPU 测试由用户运行。本地 CPU 检查、测试入口交付和历史组件结果均不能替代
当前切片的硬件验收。收到并分析结果、经用户确认后才关闭对应任务并解锁依赖。

## Out of Scope

以下为本阶段不交付的能力；其中明确安排在后续阶段的目标不因本节而取消。
推进顺序及承接缺口见[目标对齐核对](target-alignment.md)。

- dense baseline 的重复实现，以及 dense 共享 prefix、KDA 状态交接和生成缓存保存。
- sparse K1 同侧共享副本、共享 writer 并行策略与性能选型；这些保留为后续阶段。
- 远端 prefix、page-first 改造、alive 的通用散页分配、动态可追加页表。
- 多请求并发、alive hit、动态 grant、排队、wait、retract／resume、淘汰、重试和故障恢复。
- 多 chunk、逐层／逐 chunk 流水、提前返回首 token、未对齐 prompt 与混合尾页。
- sparse decode admit、D 生成 Index K 的 DRAM 写回、attention DP 扩展和异构 TP 部署。
- LoRA、多模态、logprob、投机解码、HiCache／HiSparse 组合和完整状态机框架。
- 32K／64K／128K 容量与真实并发 16 的验收目标；固定槽数或 Graph padding 不代表这些能力。
- 修改 syncfile 或补全其 HTML；当前 HTML 不包含最新 dense 设计，不作为范围裁决依据。

## Further Notes

本规格以[实施讨论](discussion.md)中的用户最新确认和[会话交接](SESSION-START.md)
指向的现行 sparse 最小设计为依据。完整版仅用于保持语义和扩展方向，不整体转成
本阶段工作量。术语沿用[项目领域文档](../../CONTEXT.md)。

当前分支为 `cryang/dev/mempool-min`，基线包含原有未提交修改；实施必须保留这些
修改。NUMA 连续分配已有 CPU 检查记录，新的硬件建池、映射与 probe 仍须验证。
用户最新“2T 都可以分配 HCCS”反馈是当前拓扑线索，不等同于满容量分配或共享服务
已验收；早期仅部分 NUMA 可用的快照不作为新的固定容量限制。

dense 后续仍以 Kimi-Linear-48B-A3B-Instruct 为首个模型。接入前核对同事交付的
baseline 版本、实际 TP／backend、状态边界与恢复证据，并按实际参数重算容量；
旧 K3 的层数和容量数字不得沿用。

任务交付遵循[阶段 review 与用户 NPU 验收流程](../ascend-mempool/verification.md)。
新任务的 blocking edges 在 to-tickets 阶段确定；旧 feature 的未关闭票保持其自身
验收范围，不因新规格发布而被关闭，也不将旧任务链无条件搬成本规格的依赖。

## Comments

### 2026-10-22：to-spec 发布

- 已沿用 grill-with-docs 中确认的冷请求首片、sparse 优先和 dense baseline 分工。
- 用户明确确认服务端到端、CPU service／TP tick、NPU 复制适配器三类测试边界。
- 已核对当前代码、原有测试入口与现行最小设计，形成完整 sparse 实施规格。
- 已检查文档结构、28 条用户故事、V1–V10 覆盖和本地引用链接；未发现缺项或失效引用。
- 本轮没有修改生产代码、发布 implementation tickets，或执行新的 CPU／NPU 测试。
- 功能实现与硬件验收均保持待完成；规格可进入下一阶段任务拆分。

### 2026-10-23：按 syncfile 核对整体目标

- 用户明确以 syncfile 的 mempool-min-demo 为最终实现目标。
- 核对确认整槽临时后端、私有副本、Main/SR、生命周期及 V1–V10 与阶段 0 一致。
- 将当前规格明确限定为 sparse 阶段 0 / L0，补充后续目标覆盖表。
- K1 仍在阶段 0 / L0 之后、阶段 1 之前；当前四票不能证明共享已交付。
- 本轮为文档校正，未修改功能范围、实施代码或验收状态。
