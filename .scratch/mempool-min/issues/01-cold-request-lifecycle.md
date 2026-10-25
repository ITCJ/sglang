# 01: 真实冷请求与显式释放复用

**Parent:** [GLM-5.2 双机 sparse mempool 最小 demo：阶段 0](../spec.md)

**What to build:** Main Router 驱动真实 P/D 完成冷请求。请求从注册、入口检查、
整槽预留和绑定开始，经过真实写回、Index K／handoff 就绪和 D 生成，最终明确归还
资源；后一个请求使用刚归还的槽，输出与自身参考一致。

**Blocked by:** None（可以先开始；关闭仍需用户 NPU 验收）。

**Status:** ready-for-agent

**State:** open

**本轮依据：** 2026-10-23 用户确认[四票详细回顾](../stage-0-ticket-review.md)，
要求按该内容重新拆票。本票仍是第一个完整功能切片；首次发布记录保留在 Comments。
角色、启动与协议落地参考[01 实施方案](../ticket-01-implementation-plan.md)。
其中模块名、端口和 CLI 均为拟定实现，交付时须用实际代码与命令核对。

## 运行范围

- Ascend NPU 双机 GLM-5.2，P／D 各 TP16、attention DP1、PP1、普通非投机 decode；
  主 KV 为 78 个独立层、compact 576 维 BF16，Index K 为 21 个 producer 层、
  128 维 BF16，无独立 scale。
- 串行、单 chunk、各 rank 私有副本、足额 reserve、fail-stop；输入为 token IDs、
  greedy，原生 radix cache 关闭。D 生成段 Index K 保留 HBM，sparse decode admit 关闭。
- S_P = 8192、S_D = 128，每侧每 rank 16 个存储槽；DRAM 与原生 HBM 统一使用 page_size=B，
  P/D 和相关池必须严格相同；当前部署默认且仅允许 B=128；生成上限 128。
- P 使用 eager；D 验证真实 NPU Graph capture／replay。Graph 补齐行数与真实
  请求数分开记录，真实常驻及执行请求数均不得超过 1。
- 沿用当前 NUMA 连续均分规则与实际机器拓扑；硬件容量、映射和可见性须有对应证据。
  dense baseline、sparse K1 和旧共享性能票不构成本票依赖。

本票 prefix 和 P alive Index K DRAM 尚未启用，注册如实标记；不预分配或虚报这些区域。

## 角色与存储合同

| 角色 | 本票职责 |
|---|---|
| Main Router | 独立 CPU Python 进程，默认放 P 机；统一入口、注册、容量账本、rid／op_id、编排、串行门及 fail-stop |
| SR_P／SR_D 协调者 | 每侧位于 rank 0 scheduler；选择本侧实际分配，分发命令并汇总全组完成 |
| SR 分片 | 每 worker 一个，包括 rank 0；持有映射、校验绑定、执行存储动作并提供设备完成证据 |
| Scheduler | 继续负责原生请求队列、batch、forward 与结果；在唯一有序 tick 中推进 SR |

拟采用 HTTP 请求入口、ZMQ 控制消息及原生 P/D 数据通道。网络线程只入队；
SR 在有序 tick 内执行操作及 TP 通信，空闲时也推进。Main 只保存和转发不透明
页描述符，设备地址由持有映射的分片解析。HTTP 创建的 Req 必须与已确认的 dispatch
匹配，直接访问后端也不能绕过分配、身份检查和串行门。

alive 使用逻辑页接口与连续整槽临时后端。S_P=8192 时，slot 0 对应逻辑页 0–63；
4096-token 请求只写前 32 页，但全部 64 页额度仍被占用。最小空闲槽指空闲槽中
编号最小者，不是最小空闲 page，也不是原生 req_pool_idx。P／D 槽号可不同，
同侧各 rank 的逻辑分配必须一致；不同 rank 持有私有物理副本。

两个 rank 位于同一 NUMA 不表示共享物理内容。SR 分片数量也不等于物理副本数；
描述符按数据种类保留副本维，给后续共享接入保留边界。此票不实现 share=1。

## 正常路径与资源归还

1. 完成 BM 映射、D Graph 初始化、原生控制握手与两侧完整注册，Main 才接收请求。
2. Main 检查 token IDs、greedy、B-token 对齐、prompt／生成上限及全部容量。
   检查失败不改变目录或账本；检查通过后一次预留四类资源。
3. SR 实际分配，收齐全部 rank 成功结果后返回 alloc_ack 和页表。两侧 dispatch
   确认后创建匹配的原生 Req；继续沿用 bootstrap room、Index K 与 handoff 通道。
4. P 真正计算并写回主 KV。全部必要 rank 的 prompt 写回完成后，SR_P 报
   ready_tokens，Main 发布位置并向 SR_D 发 ready。
5. D 的整个 TP 组等待 Router ready、Index K、handoff 三条件，首 token 同样受门控。
   D 读取 P prompt 与 D 生成段；D 槽内偏移为全局 token 位置减 prompt_len。
6. P 写回与原生发送完成后清理本请求 HBM／请求行，保留 alive 和独立完成收据。
7. D 停止提交，等待最后读写，清理原生页和行，SR_D 汇总 finished。
8. Main 归还已实际清理的 D 原生额度并下发 free。SR 检查全部本地依赖、实际归还
   alive 后才回 free_ack。两侧 free_ack 收齐、账本恢复后才允许下一请求。

| 一次预留 | 4096 输入、生成上限 128 时的实际占用 |
|---|---|
| P alive | 一个整槽，64 个 DRAM 逻辑页额度 |
| D alive | 一个整槽，1 个 DRAM 逻辑页额度 |
| D Index K HBM | 每个必要 rank 实际预留 ceil((4096+128)/128)=33 页 |
| D 原生请求行 | 每个必要 rank 实际预留一行 |

Main 管额度，SR 管实际编号，不能各自维护独立分配事实。副本容量不能相加成
可服务请求容量。P 原生 HBM 由静态 prompt 上限保证，不加入这四项账本。
D 后续 prealloc／decode 使用本请求已预留的索引，避免二次分配或按全局空闲量
重复收费。实际有效 KV 长度与预留容量分开；已用页、实际尾页的未用部分和未用整页只归还一次。

先整理旧自主分配、绑定与释放副作用，再在同一票接入显式操作。必须接管 D drain、
P DONE、早到／迟到 DONE 和取消回滚；旧 RELEASE_ACK 不等于新 free_ack。
保留旧 session／attempt／租约身份校验，不新增故障恢复协议。错误或超时停止本轮
新操作与 free，由入口结束自身进程；客户端断开不构成完成或回收证据。

## Acceptance criteria

- [ ] `page_size=B`实际传到Main、P/D worker、DRAM池、native allocator、KV池与Index K传输；逐rank严格相等。当前集成仅放行128，不支持的值及不一致配置须拒绝；页哈希、4096/5120页数、整槽额度、预留和清理由B推导。

- [ ] Main Router、SR_P、SR_D 与实际推理 worker 接通；注册反映当前真实能力，
  首片未启用的 P alive Index K／prefix 不计为可用资源。网络接收只入队，
  TP 通信在 scheduler tick 中按一致顺序推进。
- [ ] 通过真实服务完成两个内容不同的 4096-token 冷请求，生成上限 128；有实际
  decode forward 与 mempool 取数，完整输出 token IDs 与同配置的既有正式
  mempool 冷算参考比较。参考也须在目标长度运行。
- [ ] Main Router／SR 是分配和绑定的唯一来源，保留 opaque 页描述符和 worker
  映射边界。P／D 可使用不同槽号，本侧各 rank 的分配结果一致。
- [ ] 入口检查、足额 reserve 和整槽记账生效。未对齐、超长、超生成上限、容量不足
  及不支持的输入均不改变目录或账本；下一个请求不能绕过串行门。
- [ ] D 在 alloc 时实际取走所需原生行和全部 HBM 页，后续从预留范围消费；
  覆盖提前 EOS、生成上限为 1、未使用页与实际尾页清理，无二次分配、重复 free、
  虚增有效 KV 长度或因已预留资源被误算为空闲不足而触发 retract。
- [ ] D 等待 Router 的 prompt ready、全部必要 rank 的 Index K 和 handoff；首 token
  同样受门控。P 原生 HBM／请求行可先于 D finished 释放，alive 仍可被 D 读取。
- [ ] 接管 D drain、P DONE 和提前／迟到 DONE 触发的旧自动释放。D 原生清理完成后
  才报 finished；Router 下发 free，SR 实际归还后才报 free_ack 并恢复整槽额度。
- [ ] 第二请求复用前一个请求真实归还的最小空闲槽。CPU 在 service／TP tick 边界
  验证迟到 rank、重复 ack、无 free 不复用、过早完成和账本一致性。
- [ ] NPU 验证实际 P／D 数据来源、D Graph capture／replay、padding 行安全性及
  D 真实负载 ≤ 1；沿用必要的 P-only／D-only／混合复制验证，不要求每层两侧
  都有正字节传输。
- [ ] error／timeout 走 fail-stop：不继续分配或提前归还，记录失败并由自运行入口
  清理自身进程。本票交付冷请求自运行入口及证据，明确尚未覆盖 prefix 命中。
- [ ] 交付同一入口的常驻服务和冷请求验收模式，两机各执行一条命令；P 侧启动
  Main 与 P，D 侧启动 D，SR 随 worker 加载。区分映射就绪与控制就绪，注册不全
  不接收请求，自动启动过程无双方互等。
- [ ] 每次实现后立即更新目标页 D 的代码进度与验证状态，并同步 Markdown／HTML。
- [ ] 完成本票适用的 CPU／静态检查和自运行测试入口，并记录实际结果与未执行项。
- [ ] 用户完成本票要求的 NPU 验收，核对实现和证据并确认可以关闭。

## 范围边界

范围收束：不创建 prefix 数据区，不实现命中恢复或 admit，不实现共享副本优化。
01 是当前能先推进的实施任务，其硬件完成条件不因机器暂不可用而省略。

## Verification

- CPU 在 Router／service／TP tick 和原生 allocator 边界检查注册、消息入队、
  rank 一致性、实际行页占用与归还、迟到／重复回执、旧消息隔离、拒绝和 fail-stop。
  核对外部可见行为及资源归属，不以私有方法调用次数代替行为证据。
- NPU 主用例必须发生真实 decode forward；另测提前结束的清理。参考使用同输入、
  同模型、同配置、同目标长度的既有正式 mempool 冷算路径；比较完整 output IDs。
  P-only／D-only／混合适配器验证补充真实服务证据，不使用清零 P KV 的负向手段。
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
- 当前实施与验证状态同时写入[目标页 D](../target-alignment.md)。阶段 0 四票只交付
  单请求功能；共享存储接入在之后承接，不能把私有副本作为最终共享目标。

## Comments

### 2026-10-23：按已批准拆分发布

- 用户确认四票粒度及依赖关系；本票作为独立实施任务发布。
- 发布时尚未实施，尚无本票新增行为的 CPU／NPU 通过证据；全部验收项保持未勾选。
- 当前代码参考基线为 `787d10a287`；原有未提交修改必须保留，实施前核对实际工作区。

### 2026-10-23：按用户确认的详细回顾重新发布

- 用户表示“没有问题了，按照上面的内容重新拆票”；沿用四票边界和依赖，在原票内更新。
- 本次核对基线为 `3883624810` 加当前工作区；历史发布基线保留用于追溯。
- 补齐 Ascend NPU 约束、Main／SR／scheduler 分工、page／slot／副本区别、真实
  D 原生预留、旧自动释放接管、启动方式及逐步更新目标页 D 的要求。
- 本轮只重编实施票并检查文档；未实现功能、未运行新行为的 CPU／NPU 测试。
  State 保持 open，全部验收项未勾选；01 可开始，关闭仍需用户 NPU 验收确认。

### 2026-10-23：ticket01 代码实施，等待硬件验收

- 已实现独立 Main、scheduler 内 SR_P/SR_D、真实 native 行页预留、显式 ready/free、
  旧自动释放隔离与 fail-stop；DRAM sparse fetch 继续复用现有两次 unidexcopy launch。
- 新增双机 serve/acceptance 自运行入口，自动运行同长度旧正式 mempool 参考和目标
  路径，比较完整 output IDs，并检查 Graph、来源、顺序、复用和账本归零。
- 所有改动文件及三个主线接缝的必要性见
  [代码审核与运行指南](../../../ascend-mempool-test/MEMPOOL_MIN.md)。
- 单文件 CPU 行为测试和 15 个相关源文件 strict mypy 已通过；完整回归及双轴评审
  结果将在交付时追加。未运行 NPU，无本票硬件通过或用户接受证据。
- State 保持 open；验收清单不因代码就绪自动勾选，后续票继续等待 01 的用户验收。

### 2026-10-23：最终回归与评审修正交付

- 本轮 34 个文件的逐项说明见 [审核指南](../../../ascend-mempool-test/MEMPOOL_MIN.md)
  及 [可视页面](../../../ascend-mempool-test/MEMPOOL_MIN.html)。生产代码共 15 个文件，
  其中 11 个位于 Ascend/NPU。公共接缝最终为四个：decode.py、prefill.py、
  allocation.py、schedule_batch.py；前条实施记录中的“三个”由本次修正更新。
- 规格评审发现的 P 参数校验时机、逐 D step 真实负载证据、独立 timeout/fail-stop
  场景及清理后完整日志检查均已补齐；最大 8192+128 请求的 context 默认改为 8448，
  保留原生两级长度余量，并校验实际 scheduler 限制。规格最终复核没有未解决项。
- 规范评审要求的新数据容器已改为 msgspec.Struct。剩余两项非阻断维护建议：
  拆分较长编排函数、增强 active 字典的内部状态类型。详情在指南 Standards 面板。
- Mac 实际完成：完整 CPU 套件 408 项全部通过（24.358 秒）；15 个相关源文件
  strict mypy；26 个修改 Python 文件 AST/lint；shell 语法、启动 dry-run、diff
  whitespace 和 HTML STE 检查通过。lint 仅豁免基线已有的 schedule_batch.py F811。
  首次全套因缺 transformers 有 1 项无法运行，补齐仓库指定 5.12.1 后完整重跑通过。
- 自运行 acceptance 会比较四个正常请求的完整 IDs，随后暂停本轮启动的 P worker，
  验证 D 已预留资源在超时后不被归还，并在两侧进程组清理后复核最终 D 日志。
  默认该场景增加一次 600 秒等待；报告必须记录 process_cleanup_verified=true。
- **等待用户执行 NPU 验收**：尚无本票的 NPU kernel/Graph、真实双机 BM 可见性、
  模型完整输出、硬件资源复用和故障场景通过证据。未授权 agent 执行硬件测试。
  State 继续 open，硬件验收项保持未勾选，下游 ticket02 不解锁。

### 2026-10-24：统一页设计修正

用户明确决定 DRAM/native HBM、sparse/dense、P/D 和相关池共用 `page_size=B`。
现行正文已修正；此前 Comments 中的 64/128、4032 和半页接续仅为历史。
本轮 B=128 示例保持请求长度与 5120-token prefix 容量；C 命中3968、重算128。
统一公式与支持范围见 [讨论记录](../discussion.md)。本票保持 open；
01 的 NPU 验收等待用户执行，02–04 的依赖门控保持不变。

### 2026-10-24 统一页修正验证记录

- 完整CPU套件416项通过，7文件strict mypy、13文件lint/Python3.9 AST通过。
- P/D启动dry-run均传入PAGE_SIZE=128；旧HBM_PAGE_SIZE冲突返回2。
- 哈希B参数、32/40页、整槽64/1页、注册不一致及native释放由CPU覆盖。
- 图解已重算，syncfile写回按独立清单同步；详细证据见../unified-page-size-change.md。
- NPU未执行、用户未验收；State=open，不解锁ticket02。

### 2026-10-25：31/32 首次 NPU 验收停在身份比较，修复 JSON 类型误判

- 用户在 P=10.120.72.31、D=10.120.72.32 运行 f5924d557e；两侧已连接，
  随即报告 P/D code, config, runtime packages or model identity differ。
  本次尚未启动复制适配器或模型服务，不计为 NPU 功能通过。
- CPU 使用真实 Peer/socketpair 复现根因：msgspec.to_builtins 保留 numa_nodes
  的 tuple，JSON 传输后变为 list；内容相同也会直接比较失败。
- run_mempool_min.py 将配置转为实际 JSON 表示后比较，保留所有真实配置、模型、
  运行库和代码差异的拒绝条件。握手前保存 identity.json，收到对端后保存
  peer-identity.json 和 identity-diff.json；终端列出确切差异字段。
- 本次唯一功能文件位于独立 Ascend 验收入口：身份交换在服务启动前发生，
  Ascend/NPU worker 尚未装配，因此 worker hook 无法修复 runner 的类型比较。
- 验收入口 CPU 回归 10 项通过，包含相同身份通过、真实差异拒绝与断连留证；
  原型先复现 tuple/list 不等，再确认 JSON 规范化后相等。strict mypy 两文件、
  Ruff 与格式检查通过。
- Standards：0 项发现；Spec：0 项发现。两轴只读复核本补丁，不包含 ticket02。
- 修复作为 f5924d557e 之后的独立 ticket01 验收提交；ticket02 保持独立本地提交。
  用户仍在原目录运行，复用 31/32 配置并换新 run-id；等待用户重跑 NPU 验收。
  State 保持 open，尚无本票硬件通过结论。

### 2026-10-25：31/32 第二次验收停在参考日志检查，修复 freeze_gc 启动竞态

- 用户运行 93678e5fd4、run-id=ticket01-3132-20261025-02；身份比较已通过。
  参考组已启动并执行请求，P 日志显示 4096-token prefill、各 rank DONE/free=16。
  这些片段不是整票通过证据；本次尚未进入 Main/SR 目标组。
- 首个 Traceback 来自 post-warmup freeze_gc failed：本机 8000 端口拒绝连接。
  紧接着服务 ready=200，后续请求继续完成。最终 verify_service.check_logs
  扫到启动时的 Traceback，拒绝参考组，32 收到对端失败通知。
- 根因：lifespan 在 HTTP 开始监听前启动 warmup 线程。验收入口使用
  skip_server_warmup，跳过了普通 warmup 中的 HTTP 等待；随后立即请求 freeze_gc。
- 修复在公共 http_server.py 的 _freeze_gc_after_server_warmup 内，为连接错误
  增加 10 秒预算内的重试。成功后结束；持续失败、HTTP 拒绝、读超时及 TLS
  错误仍报告原始异常。保留认证、TLS 设置、GC 操作和所有验收错误检查。
- 主线改动必要性：竞态发生在 tokenizer HTTP lifespan 与监听之间，P/D 参考组
  和目标组共用此启动函数。Ascend/NPU worker hook 无法控制父 HTTP 进程的
  监听时序；启用模型 warmup 又会破坏当前显式请求和资源生命周期验证。
- 本地真实 HTTP 回归先复现首次拒绝连接时丢失 GC 请求，再确认监听就绪后成功。
  新增 6 项 CPU 测试，覆盖 skip/普通/joiner、认证/TLS、失败边界和 callback；
  参考验收器 9 项测试通过。Ruff lint/format、isort、AST、diff 检查通过。
- Spec 0 项发现；Standards 无硬性问题。已落实唯一非阻断建议，补测连接请求
  本身消耗的时间计入预算，后续 HTTP timeout 缩小且耗尽后不再重试。
- 用户硬件重跑待执行，State 保持 open。修复单独交付在 ticket01 之后，
  ticket02 继续保留独立本地提交。
