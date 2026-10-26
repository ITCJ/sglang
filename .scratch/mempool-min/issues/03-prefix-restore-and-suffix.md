# 03: 命中 prefix 后恢复与后缀计算

**Parent:** [GLM-5.2 双机 sparse mempool 最小 demo：阶段 0](../spec.md)

**What to build:** 在真实 A 保存的 prefix 上，B 命中 4096 token、C 命中 3968 token。
恢复 P 原生 HBM，填充请求自己的 alive，只计算后缀，完成 Index K／handoff 与
D 生成；输出等于各自冷算参考，释放和再次保存仍正确。

**Blocked by:** [02：请求结束后保存 prompt 到 prefix](02-prompt-prefix-save.md)。
该票完成实现 review、用户 NPU 验收并关闭后才可开始。

**Status:** needs-info

**State:** open

**实施授权：** 2026-10-26 用户在核对范围后明确要求 `$implement 开始实现ticket03`。
02 自动化 NPU 报告已通过但尚待关闭确认；本次授权仅允许先实施 03，
不代替 02 的关闭确认或 03 自身 NPU 验收，也不解锁 04。

**本轮依据：** 2026-10-23 用户确认[四票详细回顾](../stage-0-ticket-review.md)，
要求按该内容重新拆票。本票直接消费 02 已验证的真实保存数据，同时完成部分命中与
相同 prompt 的恢复、计算、生成和再次保存。

## 运行范围

- Ascend NPU 双机 GLM-5.2，P／D 各 TP16、attention DP1、PP1、普通非投机 decode；
  主 KV 为 78 个独立层、compact 576 维 BF16，Index K 为 21 个 producer 层、
  128 维 BF16，无独立 scale。
- 串行、单 chunk、各 rank 私有副本、足额 reserve、fail-stop；输入为 token IDs、
  greedy，原生 radix cache 关闭。D 生成段 Index K 保留 HBM，sparse decode admit 关闭。
- S_P = 8192、S_D = 128，每侧每 rank 16 个存储槽；DRAM 与原生 HBM 统一使用 page_size=B，
  P/D 和相关池必须严格相同；当前部署默认且仅允许 B=128；生成上限 128。
- P eager、D NPU Graph；目标组开启 match_prefix，参考组关闭并独立初始化。
- 沿用当前 NUMA 连续均分规则与实际机器拓扑；硬件容量、映射和可见性须有对应证据。
  dense baseline、sparse K1 和旧共享性能票不构成本票依赖。

prefix 位于 P 机，每 rank 一份、每份 40 个逻辑页（5120/B），主 KV 和 Index K 均按实际完成保存。

## 命中与操作合同

Main 只匹配连续 ready 前缀，命中上限为 floor((prompt_len-1)/B)×B。
同一 prompt 已有完整 prefix 时，仍保留最后 B token（本例 128）的计算与首 token 采样。
命中请求仍分配自己的 P／D alive，扣除完整槽额度；prefix 与 alive 不合并所有权。

| 操作 | 源和目标 | 完成后允许的行为 |
|---|---|---|
| load_back（原文 op2） | prefix → P 原生 HBM，包括主 KV 与 Index K | 全部必要 rank 的恢复数据可读后，P 才开始后缀计算 |
| fill（原文 op1） | prefix → 本请求 P alive，包括主 KV 与 Index K | 命中段可以计入请求 alive 的 ready_tokens |
| admit（原文 op3） | 请求结束后的 P alive → prefix | 目标可读且父路径已发布后，后续请求可以命中 |

op1／op2／op3 是文档中的操作名称，不是固定运行时 op_id。Main 为实际操作分配
单调 op_id，在计划时分别保护 load_back 和 fill 的来源，收到对应真实 ack 后各
解除一次。SR_P 协调者在有序 tick 分发、汇总；各 rank 分片使用自身映射执行 NPU
适配的复制和完成检查。分片数与物理副本数分开；本票仍逐 rank 处理私有副本。

load_back 必须使用原生 allocator 返回的真实 token 索引，将 compact 576 维主 KV
拆回 512／64 两部分，另行恢复 21 个 producer 层的 Index K。prefix_indices 是
长度等于 hit_len 的逐 token 索引，不能替换为 DRAM 或 HBM 页号列表。

## 正常路径与关键边界

1. Main 匹配前缀、预留四类资源，SR 安装独立的新请求绑定。P 的静态 prompt 上限
   仍须容纳完整 prompt 的原生 HBM，包括恢复的命中段。
2. 保护来源并执行 load_back。全部必要 rank 的主 KV 两部分和 Index K 可读后回 ack。
3. load_back 完成后，允许 fill 与 P 后缀计算独立推进。fill 写 [0,hit_len)，
   后缀写 [hit_len,prompt_len)，各自保留完成证据；是否实际重叠由 NPU 测量确认。
4. 后缀写回使用独立范围跟踪器，内部计数从零起并检查连续性。禁止伪填旧计数器，
   将命中长度当成已经完成的写回。
5. ready_tokens 只累计请求 alive 从零起连续完成的范围，并要求主 KV、Index K
   的全部必要副本完成。后缀先写完而 fill 未完成时，不能跳过前段报告完整 ready。
6. P 原生通道传输恢复与新算组成的完整 Index K，并交接首 token 和 metadata。
   D 仍等待 Router ready、全部必要 rank 的 Index K 与 handoff，整个 TP 组一致推进。
7. P 后缀写回与原生发送完成后可释放 HBM／请求行，不等待独立读取 prefix 的 fill。
   D 使用新请求 alive 的绑定真实读取；fill 未完成时仍不能开始。
8. finished 后只 admit 缺失 prefix 页。没有保存依赖的 alive 页先逻辑释放，
   整槽仍等最后复制依赖及显式 free_ack 后归还；下一请求等待完整结束流程。

| 用例 | 命中与恢复 | 新算与保存 |
|---|---|---|
| A：4096-token 冷请求 | 从空 prefix 开始，真实保存 32 页 | 为 B、C 提供来源 |
| B：A 加 1024 token | hit_len=4096，恢复 32 个 HBM 页，fill 32 个逻辑页 | 计算 1024 token；只增加 8 页，prefix 达到 40 |
| C：与 A 相同 | hit_len=3968，恢复 31 个完整 HBM 页（3968 token） | 最后 128 token 在第 32 页计算；不重复 admit，prefix 仍为 40 |

C 的 prefix_indices 必须恰有 3968 个有效索引，不含尚未恢复的最后 B token（本例 128）。
按原生映射验证非连续分配和完整页边界接续，不用改变命中上限回避边界。
D Index K 仍按完整 prompt 加生成上限预留：B 为每 rank 41 页，C 为 33 页。
P alive 每次占 S_P/B=64 页整槽额度，D alive 占 S_D/B=1 页整槽额度。

## 失败与独立演示

延迟 fill 或任一 rank 完成时，D 不得提前开始；重复 ack 不重复解锁或发布。
恢复、复制或推理失败走既有 fail-stop，停止新操作与 free，不换源或在本轮重试。
旧 DONE／drain 自动归还路径继续由 Main／SR 接管。

演示从真实 A 保存开始，运行 B、C，并分别与同配置关闭 match_prefix 的冷算结果
比较完整 output IDs。参考组与目标组独立初始化，避免参考计算污染命中与容量。
本票交付命中测试与报告；04 再组合满缓存 E、全部拒绝及失败场景。

## Acceptance criteria

- [ ] Main Router 匹配连续 ready 前缀，按 B-token 逻辑页执行命中上限；B 的
  hit_len 为 4096，C 为 3968。每次仍分配独立的请求 alive，保留整槽额度。
- [ ] op2 load_back 使用原生 allocator 与 token 映射，将 compact 主 KV 恢复到
  512／64 两部分，另行恢复 Index K。每个必要 rank 的数据可读后才 ack。
- [ ] prefix_indices 是逐 token 索引，长度等于 hit_len。C 在 128-token 原生页
  中恢复 31 个完整页；最后 128 token 在第 32 页计算，
  不通过改变命中上限回避此边界。
- [ ] op2 ack 后允许 op1 fill 与 P 后缀计算独立推进。suffix 写回使用从零计数的
  独立范围；ready_tokens 汇总 op1 与后缀的真实完成，不伪填旧计数器，也不
  等待结束后的 prefix admit。
- [ ] 源在操作计划时被保护，在真实 ack 后按 op_id 解除一次；延迟 fill 或任一
  rank 的完成不能使 D 提前开始。P native 释放不依赖 op1，所有实际读写依赖
  仍须满足，旧自动释放不能重新介入。
- [ ] 每 rank 按逻辑内容核对 fill 后 alive、load_back 后 HBM 两部分与 Index K，
  包括非连续原生映射和完整页边界接续。D 使用新的请求绑定真实读取 P／D mempool。
- [ ] B、C 的完整输出序列分别与自身冷算参考比较。B 只 admit 缺失后缀；C 不重复
  保存，prefix 占用保持 40 页。任何误差放宽按规格校准并报告，不能以首 token
  相同代替完整输出检查。
- [ ] CPU 覆盖两段就绪、迟到／重复 ack、源保护和失败路径；NPU 完成复制内容、
  真实命中推理、D Graph 与释放检查。扩展自运行入口提供命中阶段的测试。
- [ ] 同一启动入口自动生成独立冷算参考与真实保存来源，运行 A／B／C 并保存
  hit_len、prefix_indices、两段进度、完整输出和复制证据；保留 01／02 的释放、
  账本、真实串行与满容量行为。
- [x] 每次实现后立即更新目标页 D 的代码进度与验证状态，并同步 Markdown／HTML。
- [x] 完成本票适用的 CPU／静态检查和自运行测试入口，并记录实际结果与未执行项。
- [ ] 用户完成本票要求的 NPU 验收，核对实现和证据并确认可以关闭。

## 范围边界

范围收束：仍为每 rank 私有副本、本地 prefix、串行、单 chunk；不加入共享读取、
远端 prefix、逐层流水或新的并发状态机。

## Verification

- CPU 在 service／TP tick 和原生分配边界验证 load_back 先完成、fill／后缀分别
  推进、连续 ready、重复 ack、迟到 rank 与 实际尾页归还，覆盖先释放 P native
  后 fill 完成的顺序，核对真实资源归属。
- NPU 逐 rank、逐必要层、逐逻辑 token 核对 fill 后 alive、load_back 后 HBM 的
  512／64 两部分和 Index K，按真实映射比较；不以布局不同的整块校验和代替。
  D Graph 与实际 P／D 数据来源、P native 提前释放及后续正确输出均需本票证据。
- B、C 的完整 output IDs 各与自己的冷算参考比较；确定性选项须核实对 DSA／
  自定义 kernel 的实际覆盖。任何误差容限以该用例自身的重复冷算校准，并明确限制。
- 优先复用 service／TP tick 的 CPU 行为测试、双机 GLM-5.2 自运行入口和必要的
  NPU 复制适配器测试。观察外部行为、实际完成与资源账本，不仅核对内部调用。
- 入口自动完成启动、健康检查及注册就绪、请求、断言、报告收集和自身进程清理；
  双机允许每机一条命令，后续自动协调。失败返回非零，日志与报告保存到
  `/home/cryang/log/` 下的独立运行目录。
- 区分 agent 实际执行的 CPU／静态检查、用户 NPU 结果、smoke 与完整验收。
  报告保留版本及工作区差异标识、部署与模型身份、实际输入／输出、rank 覆盖和
  rid／op_id／槽位关联；任何未执行或缺失证据都明确标记。
- 实施后向用户说明正常路径、所有权、同步和释放条件，交付可直接运行的硬件
  命令、失败判据及回传材料。实际命令以交付时已实现的 CLI 为准。
- 按[阶段交付与用户 NPU 验收流程](../../ascend-mempool/verification.md)，用户执行
  硬件测试并确认后才勾选硬件项、设置 State 为 closed、解锁下游。代码和测试
  入口就绪而硬件未验收时保持 open，并在 Comments 记录等待项目。
- 同步[目标页 D](../target-alignment.md) 的进度与证据；本票恢复／fill 的新 NPU
  复制方向和完整页边界接续，不能由 02 保存通过或历史组件报告替代验收。

## Comments

### 2026-10-26：C 差异定位到重算尾页，交付临时探针供下一轮诊断

- 用户回传 A/B 与参考完整输出相等；C 在 output[15] 首次不同，参考 624、目标 13。
  冷算 A/C 全部相等，目标 A/C 在同一位置不同。
- 用户离线运行 reuse 内容检查通过：16 rank、78 主层、21 Index 层，以及
  非连续 native 映射、native_freed 后 fill 等边界。各 rank A/C 的前 31 页
  alive 摘要相等，仅重算的第 31 页不同。
- 这证明既有数据搬运检查通过，不能证明重算尾页与冷算数值等价。
  最终综合 Graph/生命周期汇总及 timeout 检查仍未完成。根因尚未确定。
- 增加 `--diagnose-reuse` 临时探针，保持相同 A/B/C 和严格输出断言。
  仅 target P 记录实际 attention 长度、位置与页表，并逐层比较相同输入的尾页。
  诊断保留一页 CPU 副本；对现有验证数据做统计，会增加 CPU 检查与同步开销，
  不能用于性能结论。失败退出也打印简短结果并保存 reuse-diagnostic.json。
- 功能接线全部在 Ascend/NPU；双机入口和离线摘要位于既有验收脚本目录，
  需要服务外完成参数协调、进程退出后的日志收集，服务内 hook 无法替代。
- 这是诊断交付，不是根因修复。Mac 测试验证探针，不复现 NPU 模型计算。
  诊断开关默认关闭，`[DEBUG-ticket03-reuse]` 探针在确认修复后删除。
  03 保持 needs-info/open；04 不解锁，未放宽输出比较。
- 最终 CPU 回归 459/459 通过（38.333 秒），5 模块 strict mypy、8 文件
  lint/format/isort/Python3.9 AST 通过。Standards 0 项；Spec 的摘要证据关联
  问题经六个先红后绿反例修正，最终 0 项未解决。
  [本轮检查](../verification/ticket03-001-20261026/probe-checks.json)与
  [review](../verification/ticket03-001-20261026/probe-review.md)保留范围和限制。

### 2026-10-26：ticket03-001 NPU 首轮失败，正在收集 C 的差异证据

- 用户回传 31/32 终端截图，两侧均为 `0308cd0814`，使用 `reuse-acceptance`。
  P 报 `reuse-prefix-c: full output IDs differ from formal reference`，D 报 peer failed。
- 入口已进入 target；按执行顺序，A/B 的输出比较未触发错误。
  C 在最终内容/生命周期汇总与 timeout 验收前退出，不能将这些项记为通过。
- 已请求从本次保存的 responses/snapshots 提取首个差异位置、冷算 A/C 对比，
  并离线执行现有 reuse 内容检查器。当前只有截图，尚无原始序列和 rank 收据。
- [失败记录](../verification/ticket03-001-20261026/failure-record.json)区分观察、
  依据代码的推断与未知项。未放宽完整输出比较，未修改功能代码或要求重跑模型。
- State 保持 open，04 不解锁。待证据确认根因后独立提交修正与重测入口。

### 2026-10-26：实现与 CPU 验证交付，等待用户执行 NPU 验收

- 已实现连续 ready 前缀匹配、真实 allocator load_back、flat/NZ MLA 布局、
  独立 fill、suffix 从零计数、全 TP 联合 ready 与 P native 提前释放。
- 实际 Req/extend/ChunkCache 方法验证 C 的 31 页恢复、最后一页计算和整行归还。
  缺 rank、错误索引、未完成读、提前 ready 均 fail-stop 并保留资源。
- 一键入口新增 `--mode reuse-acceptance`，自动独立冷算参考与 A/B/C 目标组；
  逐 rank 核对内容和索引，完整 output IDs 必须相等，保留 D Graph、释放与超时检查。
- 验收模式明确注入非连续原生映射和 native_freed 后再提交 fill，便于证明独立释放。
  普通 serve 且 verify_prefix=false 不注入；未将这种控制解释为性能重叠证据。
- Mac 最终完整 CPU 453/453 通过（38.513 秒）；12 模块 strict mypy、23 Python 文件
  lint/format/isort/Python3.9 AST 通过。review 修正后定向回归及完整套件再次通过。
- Standards / Spec 最终均为 0 项未解决发现；Standards 的 1 项进度摘要问题已修复。
  [检查与 review 记录](../verification/ticket03-cpu-20261026/checks.json)保留本轮证据。
  [交付说明](../ticket03-delivery.md)给出
  功能源码阅读顺序、所有权/同步条件、每机一条命令与日志回传清单。
- 本机未执行 NPU，本票硬件验收全部保留。State=open，等待用户执行 NPU 验收；
  不代替 02 关闭确认，不解锁 04。硬件修正另行提交。

### 2026-10-26：用户授权实施 ticket03

- 实施基线为 `87157b7365`，保留现有未提交文档和证据。
- 按用户本轮明确指令实施 03；记录 02 尚未关闭的例外，不修改 02 验收状态。
- 测试沿用本票已约定的 service／TP tick、原生分配、NPU 复制适配器和
  双机自运行入口边界。以这些已批准边界执行 TDD，不另加测试门槛。
- 依次实现复制与原生映射、Main／SR 命中协议、后缀进度及就绪／释放、
  自运行命中验收。每步立即更新目标页 D；实现完成后进行两轴 review。
- 03 实现与后续硬件修正保留独立提交，State 保持 open，硬件由用户执行。

### 2026-10-23：按已批准拆分发布

- 用户确认四票粒度及依赖关系；本票作为独立实施任务发布。
- 发布时尚未实施，尚无本票新增行为的 CPU／NPU 通过证据；全部验收项保持未勾选。
- 当前代码参考基线为 `787d10a287`；原有未提交修改必须保留，实施前核对实际工作区。

### 2026-10-23：按用户确认的详细回顾重新发布

- 用户确认详细回顾后授权重新拆票；保持部分命中与完全相同 prompt 在同一票，直接依赖 02。
- 本次核对基线为 `3883624810` 加当前工作区；保留历史发布记录。
- 补齐 Ascend NPU load_back／fill 的分工、逐 token 索引、4032 半页接续、两段就绪、
  独立参考组和源保护的明确合同，纳入目标页 D 的进度更新要求。
- 本轮仅更新实施票并检查文档；未实现功能或执行新行为的 CPU／NPU 测试。
  State 保持 open，验收项未勾选；等待 02 用户 NPU 验收并关闭。

### 2026-10-24：统一页设计修正

用户明确决定 DRAM/native HBM、sparse/dense、P/D 和相关池共用 `page_size=B`。
现行正文已修正；此前 Comments 中的 64/128、4032 和半页接续仅为历史。
本轮 B=128 示例保持请求长度与 5120-token prefix 容量；C 命中3968、重算128。
统一公式与支持范围见 [讨论记录](../discussion.md)。本票保持 open；
01 的 NPU 验收等待用户执行，02–04 的依赖门控保持不变。
