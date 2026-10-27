# 02: 请求结束后保存 prompt 到 prefix

**Parent:** [GLM-5.2 双机 sparse mempool 最小 demo：阶段 0](../spec.md)

**What to build:** 完成一个冷请求后，将该请求的 prompt 主 KV 和 Index K 保存为
可跨请求保留的 prefix。P 原生 HBM 和请求 alive 都释放后，prefix 仍保持正确内容；
重复保存与容量不足均按目录和账本处理。

**Blocked by:** [01：真实冷请求与显式释放复用](01-cold-request-lifecycle.md)。
该票已于 2026-10-26 经用户确认 NPU 验收成功并关闭，前置条件已满足。

**Status:** needs-info

**State:** open

**最新验证：** 2026-10-26 用户回传 `ticket02-002` 双侧完整 summary 和 P target evidence，
`87157b7365` 的自动化 NPU 验收通过。保存功能项已核对；等待用户确认关闭。
物理 NUMA 落点未验证，原始报告的 `user_accepted=false` 保留。

**本轮依据：** 2026-10-23 用户确认[四票详细回顾](../stage-0-ticket-review.md)，
要求按该内容重新拆票。本票在已验收冷请求链上增加保存，不消费 prefix 命中。
2026-10-26 复核基线为 `7275f29a05`；旧 ticket02 实现已撤回，从该基线重写。
功能范围与依赖顺序不变，沿用[规划中的 warmup 回归约束](../ticket-plan.md#warmup-regression)。
具体文件职责、复制完成协议与测试安排见[代码修改计划](../ticket-02-implementation-plan.md)。

## 运行范围

- Ascend NPU 双机 GLM-5.2，P／D 各 TP16、attention DP1、PP1、普通非投机 decode；
  主 KV 为 78 个独立层、compact 576 维 BF16，Index K 为 21 个 producer 层、
  128 维 BF16，无独立 scale。
- 串行、单 chunk、各 rank 私有副本、足额 reserve、fail-stop；输入为 token IDs、
  greedy，原生 radix cache 关闭。D 生成段 Index K 保留 HBM，sparse decode admit 关闭。
- S_P = 8192、S_D = 128，每侧每 rank 16 个存储槽；DRAM 与原生 HBM 统一使用 page_size=B，
  P/D 和相关池必须严格相同；当前部署默认且仅允许 B=128；生成上限 128。
- P eager、D NPU Graph；测试关闭 match_prefix，始终从冷请求计算得到保存源。
- 沿用当前 NUMA 连续均分规则与实际机器拓扑；硬件容量、映射和可见性须有对应证据。
  dense baseline、sparse K1 和旧共享性能票不构成本票依赖。

prefix 位于 P 机，每 rank 一份、每份 40 个逻辑页（5120/B），主 KV 和 Index K 均按实际完成保存。

## 数据与角色合同

| 数据或模块 | 本票新增行为 |
|---|---|
| P alive Index K DRAM | 每 rank 增加 21 层 × 16 槽 × S_P token × 256 B 的区域，随真实计算写回 |
| prefix 池 | 每 rank 一份主 KV 与 Index K，layer-first，真正按 B-token 逻辑页分配 |
| Main Router | token 页哈希、命名空间种子、父关系、位置状态、源锁、admit 决策和 publish |
| SR_P 协调者 | 分配 prefix 页、向全部分片下发一致操作、汇总目标可读和实际归还 |
| SR_P 分片 | 在本 rank 的映射内复制主 KV 与 Index K，并提供实际完成收据 |

复用 01 的请求、账本、显式 free 和有序 scheduler tick。rank 0 同时持有协调者和
本地分片；协调者必须收齐全部必要 rank 的结果。复制接口按 Ascend NPU 映射能力
实现，只有合法 CPU 映射才可使用 CPU 复制；不能直接解引用 BM 设备地址。

启用后按数据类型分别注册 R_am、R_ai、R_pm、R_pi，本阶段均为 16。
它们是物理副本数量；每份 prefix 的 40 页不能合计为 640 页独立逻辑内容。
同 NUMA 的 rank 仍各持私有副本。P／D alive 继续整槽分配，prefix 的页号、
分配器与容量账本独立于 alive。命名空间包含 extra_key／cache_salt 的既定语义。

## 正常保存路径

1. 冷请求的主 KV 和新增 Index K DRAM 写回真实完成，原生传输完成后，P 释放
   本请求 HBM／请求行。新增 Index K 写回必须纳入这个释放条件。
2. D 完成最后读写与原生清理，SR_D 报 finished。Main 重新检查 prompt 页目录，
   跳过已有 ready prefix 位置的页，按实际容量确定待保存范围。
3. 先对待复制 alive 源及所需父路径建立保护，再解除请求 owner hold。
   没有保存依赖的页可逻辑释放，整槽等待最后一项依赖结束。
4. SR_P 分配 writing prefix 目标。每个分片复制自己那份 78 层主 KV 和
   21 个 producer 层 Index K；设备提交成功不等于目标可读。
5. 协调者收到全部必要 rank 的目标可读证据后回 admit ack。Main 先发布父页，
   再发布子页；按 op_id 解除源和父路径保护，每个 ack 只处理一次。
6. 满足回收条件的 alive 由 Main 显式 free。SR 实际归还后回 free_ack，恢复整槽
   额度；prefix 独立保留。admit 完成且两侧 free_ack 收齐后才进入下一请求。

源页只有 owner hold 时 lock_ref=1；计划 admit 后为 2；解除 owner hold 后仍为 1。
只有复制完成、最后保护解除后才可归还源槽。目录位置保留 writing／ready／draining／
freed 状态；完成收据按 rid／op_id 独立存活，不能依赖已释放的原生请求行。

## 本票可独立演示的保存用例

所有用例均关闭 match_prefix；以下数值是保存行为，不是命中验收。

| 冷算用例 | 输入 | 新保存页数／份 | 结束时 prefix 页数／份 |
|---|---|---|---|
| A | 4096 token | 32 | 32 |
| B | A 的 4096 token 加 1024 个新 token | 8，跳过已有 32 页 | 40 |
| C | 与 A 相同 | 0 | 40 |
| E | 不同内容的 4096 token | 0，容量已满 | 40 |

容量只能保存部分缺失页时，从尾部缩短范围；零容量时跳过 admit，正常归还 alive。
不引入 evict 或等待容量协议。admit 错误或超时进入 fail-stop；停止新操作与 free，
不强行释放 writing 目标或受保护源，不在本轮复用可疑数据。

## Acceptance criteria

- [x] 接入每 rank 的 P alive Index K DRAM 写回与 P 侧 prefix 页分配，覆盖主 KV
  78 层、Index K 21 个 producer 层。D 生成 Index K 继续只留 HBM。
- [x] 将 Index K 写回真实完成纳入 P 原生资源释放条件；在 P HBM 释放之后，仍能
  从请求 alive 保存完整 prompt。prefix 和 alive 的分配、位置与容量独立。
- [x] 通过 Main Router 计算 token 页哈希和命名空间种子，形成父哈希、位置与完成
  状态。冷 A 结束并释放 alive 后，每份 prefix 保留 32 页，逐逻辑 token 核对
  各必要层的主 KV 与 Index K。
- [x] finished 后重新检查目录，已存在的 ready prefix 页跳过。建立 admit 源锁与
  所需父路径保护后才解除 owner hold；只有目标可读 ack 到达且父页已发布时
  才 publish。重复 ack 不重复解锁或归还。
- [x] 复制尚未完成时，源槽不能收到 free_ack 或被复用；admit 失败进入 fail-stop，
  不强行释放 writing 目标或仍受保护的源。设备完成收据不依赖已释放的原生请求行。
- [x] prefix 容量按真实页计算。容量不足时从尾部缩短或跳过保存；重复冷算相同
  prompt 不增加已有页，缓存满时仍能正常结束并归还 alive，不引入 evict。
- [x] 此票的保存验证使用 match_prefix 关闭的冷算请求。可用 A／B／C／E 的冷算
  变体验证 32 → 40 → 40 → 40、已有页跳过和满容量行为，但不宣称这些请求
  已按完整 demo 的预期命中。
- [x] CPU 覆盖源保护、publish、重复 ack、容量和错误行为；NPU 核对实际复制内容、
  P native 提前释放及 alive 归还后 prefix 的独立存活。扩展同一自运行入口
  提供保存阶段的测试与独立报告。
- [x] 保持 01 的真实冷请求输出、D 三条件门控、显式释放、Graph 和真实串行约束；
  直接复用同一启动入口，注册如实启用新增区域并检查实际容量。
- [x] 每次实现后立即更新目标页 D 的代码进度与验证状态，并同步 Markdown／HTML。
- [x] 完成本票适用的 CPU／静态检查和自运行测试入口，并记录实际结果与未执行项。
- [ ] 用户完成本票要求的 NPU 验收，核对实现和证据并确认可以关闭。

## 范围边界

范围收束：计算仍走冷请求，命中消费留给 03；本票的目录与保存数据直接由 03 复用。

## Verification

- 保留已验收的原生 fake warmup：启用 prefix 后，P/D warmup 完成仍不占 Router
  额度、不写 alive／prefix、不产生 admit／publish；随后真实冷请求正常保存和释放。
  真实请求缺少预留、fake/real 混批仍严格拒绝；新增 Index K 写回只接纳有效真实绑定。
- CPU 覆盖“先保护源、再解除 owner hold”、父页发布顺序、延迟一个 rank、重复 ack、
  部分容量缩短、满容量跳过以及错误／超时。测试检查源仍受保护和账本的真实变化。
- NPU 在 P native 已释放后执行保存，并在 alive 实际归还后核对 prefix 的逻辑内容。
  每个必要 rank、每个必要层同时核对主 KV 与 Index K；保留源数据参考及完成证据。
- 在 01 的常驻服务／自运行入口上增加保存验收模式：自动冷算 A／B／C／E、检查
  32 → 40 → 40 → 40、收集两机报告并清理。报告明确尚未覆盖命中推理。
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
- 同步[目标页 D](../target-alignment.md) 的进度与证据；01 的通过记录是前置基础，
  不能替代本票新增 Index K 写回、保存与生命周期的 NPU 验收。

## Comments

### 2026-10-23：按已批准拆分发布

- 用户确认四票粒度及依赖关系；本票作为独立实施任务发布。
- 发布时尚未实施，尚无本票新增行为的 CPU／NPU 通过证据；全部验收项保持未勾选。
- 当前代码参考基线为 `787d10a287`；原有未提交修改必须保留，实施前核对实际工作区。

### 2026-10-23：按用户确认的详细回顾重新发布

- 用户确认详细回顾后授权重新拆票；本票继续直接依赖 01，未新增外部 blocker。
- 本次核对基线为 `3883624810` 加当前工作区；历史记录保留。
- 补齐 Ascend NPU 数据区域、SR 分工、源锁与 publish 顺序、冷算保存矩阵及启动交付。
- 本轮仅更新实施票并检查文档；未实现功能或执行新行为的 CPU／NPU 测试。
  State 保持 open，验收项未勾选；等待 01 用户 NPU 验收并关闭。

### 2026-10-24：统一页设计修正

用户明确决定 DRAM/native HBM、sparse/dense、P/D 和相关池共用 `page_size=B`。
现行正文已修正；此前 Comments 中的 64/128、4032 和半页接续仅为历史。
本轮 B=128 示例保持请求长度与 5120-token prefix 容量；C 命中3968、重算128。
统一公式与支持范围见 [讨论记录](../discussion.md)。本票保持 open；
01 的 NPU 验收等待用户执行，02–04 的依赖门控保持不变。

### 2026-10-25：用户撤回提前实施，旧实现移除并等待重写

- 用户明确要求删除未通过 NPU 验收的旧 ticket02 提交及实现；当前分支已移除。
- 原提交保留在本地恢复引用，未推送到远端，不作为当前实现或验收依据。
- 先完成 ticket01 的 warmup 适配和 NPU 验收，再基于稳定基线重写 ticket02。
- State 保持 open，全部硬件验收项未通过；此前提前实施授权不继续生效。

### 2026-10-26：ticket01 已验收，复核后保持保存范围

- 用户确认 ticket01 验收成功，01 已关闭；本票从 `7275f29a05` 已验收基线重写。
- warmup 改动未改变真实请求的保存源、生命周期或页模型，02–04 无需重新拆分。
  新增 warmup 隔离回归要求见 Verification，03／04 沿用任务索引中的共同约束。
- 本轮只核对规划与前置状态，没有恢复旧实现或开始新实现。
  State 保持 open，新增行为的 CPU／NPU 验证仍待实现后执行。

### 2026-10-26：用户授权重新实现，形成代码修改计划

- 用户要求“接下来可以开始重新实现ticket02了，规划一下代码修改”。已基于当前
  `7275f29a05` 调用链形成[实施计划](../ticket-02-implementation-plan.md)与
  [HTML](../ticket-02-implementation-plan.html)，本轮尚未修改功能代码。
- 预计修改 8 个现有功能文件、新增 `npu/mempool/prefix.py`，均位于 Ascend/NPU。
  复用既有 attention hook、service 完成条件、原生 warmup 和公共分配入口。
- 关键工作包括 Index K 写回完成门控、持久 prefix 目录和异步 admit 的全 TP 确认。
  P BM 分配按现有对齐为 13 GiB/rank、TP16 共 208 GiB；测试入口同步容量检查。
- 测试沿用已确认接口，按行为切片 red → green；交付前执行 CPU／静态检查及两轴 review。
  用户 NPU 验收后才关闭本票，03 继续保留依赖门控。

### 2026-10-26：按用户 $implement 授权完成重写，CPU 通过

- 基于已验收 ticket01 `7275f29a05` 实现；8 个既有功能文件与 1 个新增文件均在
  Ascend/NPU。新增 P alive Index K、40 页 prefix、全 TP admit、目录保留与显式 free。
- 旧 ticket02 提交未恢复；复用当前 attention/service/warmup 接线，没有主线功能改动。
- 完整 CPU 套件 437/437 通过，35.966 秒；首次全量缺本地 requests 依赖，
  补回已有缓存路径后完整重跑通过。13 个源/脚本 strict mypy、21 个 Python 文件
  lint/format/isort/Python3.9 AST 检查通过；验收 checker 拆分后定向测试通过。
- 自运行 `--mode prefix-acceptance` 已就绪：独立参考、A/B/C/E 冷算、TP16 逐值内容、
  实际槽页/回执/目录关联、32→40→40→40、故障与进程清理。
  [交付说明](../ticket02-delivery.md)包含功能 review 顺序和两机命令。
- Standards/Spec 最终复核均为 0 项剩余发现。两项可读性建议已整改；
  验收描述符短列表问题经 red→green 修复。整改后相关 42 项定向测试及类型检查通过。
- 尚未执行本票 NPU 验收，功能硬件项保持未勾选。
  Status=needs-info 表示等待用户 NPU 报告；State 保持 open，03 不解锁。

### 2026-10-26：修复首轮 NPU reference 检查矩阵不匹配

- 用户回传 31/32、run-id=`ticket02-001` 的控制台截图：reference 阶段报
  `require zero-decode and at least two real decode requests`，D 收到 peer failed。
  尚未进入 target；本次反馈不构成 prefix 硬件验收通过。
- 根因是复用的 formal checker 无条件要求至少一个 zero-decode，而本票 A/B/C/E
  都强制生成 128 token。CPU 通过真实 Runner.stage、checker 与进程清理复现相同异常。
- `verify_service.py` 增加显式完成计数合同；`run_mempool_min.py` 仅在
  prefix-acceptance 的 reference 传入 0 个 zero-decode，并要求准确 4 个 completed。
  默认旧矩阵要求保留，全部 rank、Graph、写回、drain、释放和槽复用检查继续执行。
- 本次修改 2 个验收脚本、2 个测试文件，无生产功能源码改动。CPU 完整套件
  440/440 通过（36.115 秒），2 文件 strict mypy、4 文件 lint/format/isort/AST 通过。
  Standards / Spec 分别复核，均为 0 项发现。
- [检查记录](../verification/ticket02-reference-matrix-fix-20261026/checks.json)保存 red/green、
  完整 CPU 日志与验证版本摘要。两机更新后使用已有
  `/home/cryang/mempool-min-ticket01-3132.json`，换新 run-id=`ticket02-002` 重跑。
  保留 `ticket02-001` 失败日志；NPU 重测仍待用户执行，02 保持 open。

### 2026-10-26：ticket02-002 自动化 NPU 验收通过

- 用户回传完整 P/D summary 和 P target evidence；两侧均为 `passed`，完成
  fetch、reference、target。原字段保存在[本轮证据目录](../verification/ticket02-002-20261026/record.json)，
  来源是会话粘贴，未直接读取服务器文件，也未收到完整日志包。
- 两侧代码版本均为 `87157b7365a78aa336d9558e1ce9f820bee2fdd4`，identity 无差异，
  受检功能路径无 tracked diff、无 untracked 文件。P/D 为 10.120.72.31/32，
  GLM-5.2-w4a8、bond4、各 TP16，page_size=128、prefix_pages=40、verify_prefix=true。
- 命令使用 `--mode prefix-acceptance`、配置
  `/home/cryang/mempool-min-ticket01-3132.json`、run-id=`ticket02-002`；
  日志根目录为 `/home/cryang/log/mempool-min/ticket02-002/`。
- A/B/C/E 共 4 个冷请求，各份 prefix 页数为 32→40→40→40。16 ranks、78 层主 KV、
  21 层 Index K 均核对 `original == alive == prefix == retained after alive free`。
  完整输出 token IDs 与同长度独立参考一致，D Graph 与 P/D BM 数据来源检查通过。
- 两侧故障注入均观察到 `SR heartbeat timeout`；保留 16 ranks 及全部额度，
  `automatic_free=false`，随后本次所属进程组清理通过。该记录是预期故障验收通过。
- 前述 CPU 440/440、静态检查和两轴 review 证据继续有效。本次仅归档用户硬件结果
  与更新文档，未修改功能代码，也未重新执行 CPU/NPU 测试。
- 保存功能验收项已勾选。报告保留 `user_accepted=false`；本次用户回传结果，尚未
  明确确认关闭，因此 State=open、Status=needs-info，03 继续等待关闭确认。
- `numa_placement="not verified"` 是当前 mempool-min checker 的固定字段，未执行
  实际落点探测。NUMA 请求参数已接入 BM，但本次不据此声明物理页落点已验证。
  旧共享探针曾遇到 davinci_manager 映射无节点页计数；这不是本轮新测得的错误。
