# mempool-min 实施讨论

日期：2026-10-22。第 1 轮阶段：grill-with-docs。

本文件记录实施侧新确认的范围、分工和切片边界。设计依据仍为
[SESSION-START.md](SESSION-START.md) 指向的现行最小版设计；syncfile 只读。
这里的确认用于后续 to-spec、to-tickets，不代表已经实现或通过验收。

## 第 1 轮：首片边界与两条轨道的分工

### Q1：先完成真实冷请求闭环，再加入 prefix 复用

用户确认：可以。

首个可验收切片为：Main Router → SR → 真实 P/D 冷请求 → finished →
实际 free_ack → 第二个冷请求复用已归还的存储槽。

- 使用双机 GLM-5.2 sparse mode，沿用每侧 TP16、attention DP1、串行、
  单 chunk、足额 reserve 和 fail-stop 的已确认约束。
- 首片覆盖真实推理与 mempool 读写、统一分配与绑定、就绪门控、正常结束、
  实际归还和槽位复用。每 rank 私有 alive 池的现有数据路径继续复用。
- 首片就落实 finished 与 free_ack 的区别。原生 HBM／请求行清理与 alive
  存储槽释放分开；旧路径在 D drain 或 P 接收 DONE 后自动归还槽的行为，
  在新模式中由 Main Router／SR 的明确 free 流程接管。
- P/D 原生连接继续承担 Index K 与 handoff 附属信息；所有相关 rank 的
  prompt KV、Index K、handoff 就绪后，D 才开始输出和 decode。
- prefix 保存和命中复用留给后续切片。首片完成仅证明冷请求链路与生命周期，
  不等于完整 sparse 最小 demo 的 V1–V10 验收完成。

代码核对依据：现有控制协议的
`finish_drain`、`_accept_done`／`_release_prefill`、以及提前收到 DONE 后的
`finish_prefill_writes` 路径会归还存储槽；原生资源清理与 runtime 行脱离已有
可复用边界。后续加入 prefix 保存时，P alive 的 Index K 写回必须与保存能力
一起接入，因为 P 原生 HBM 可以在 D 结束前释放。

### Q2：dense baseline 由同事并行推进，本地先做 sparse

用户确认：dense 的模型运行、边界状态捕获与恢复相关 baseline 工作，已经交给
其他同事在 SGLang baseline 上同步实现；本仓库当前优先实现双机 GLM-5.2
sparse mode。

- 本地当前实施主线是 sparse，dense baseline 不构成 sparse 首片的前置依赖。
- dense 仍是后续目标，首个模型保持 Kimi-Linear-48B-A3B-Instruct。
  同事的 baseline 工作不自动等于本仓库的共享 prefix、saved 后分配 D、
  跨机恢复与生成缓存复用已经完成。
- 后续进入 dense 集成前，接回并核对 baseline 的代码版本、实际模型配置、
  NPU 后端与 TP、状态 dtype／形状／边界，以及恢复后的输出和资源归还证据。
  实际完成范围以交付结果为准。
- 本轮不增加 dense baseline 的重复实现任务，也不要求 sparse 首片先完成
  sparse K1 共享优化或旧共享性能票。

## 阶段边界与证据

- 用户要求按阶段推进：先 grill-with-docs，再 to-spec、to-tickets，之后按票实现。
  本轮只记录已确认决定；尚未生成实施规格、发布 tickets 或修改生产代码。
- 第 1 轮的两个问题已确认。现行最小版中其他已确认设计继续沿用，不重新访谈。
- 讨论阶段没有执行新的 CPU 测试或 NPU 测试；代码存在与硬件验收分开记录。
- 后续验收沿用[阶段交付与 NPU 验收流程](../ascend-mempool/verification.md)：
  本地检查与自运行测试入口可先准备，硬件由用户运行；硬件验收通过并经用户确认后
  才关闭对应 ticket、解锁依赖。新 demo 的任务范围与依赖在 to-tickets 阶段确定，
  不将旧 feature 的整个任务链自动搬入本地 sparse 主线。
- 后续测试应自动完成启动、就绪、请求、断言、报告与自身进程清理；双机允许每机
  一条命令并自动协调，NPU 日志和报告写入 `/home/cryang/log/`，失败返回非零。

下一阶段为 to-spec：将本轮确认的 sparse 主线与现行最小版设计整理成实施规格，
明确首片的服务级验收边界；任务拆分另在 to-tickets 阶段审阅。

## 2026-10-22：to-spec 阶段记录

用户调用 to-spec，并确认真实服务端到端、CPU service／TP tick、NPU 复制适配器
三类测试边界。已发布[完整 sparse 实施规格](spec.md)，Status 为 ready-for-agent、
State 为 open；首片与完整 demo 的验收分别定义。后续单独进入 to-tickets，
当前尚未发布实施票或修改生产代码。

## 2026-10-23：详细回顾确认与重新拆票

用户核对[阶段 0 四票详细回顾](stage-0-ticket-review.md)后表示：
“我没有问题了，按照上面的内容重新拆票吧”，并显式调用 ask-matt。
本轮按 to-tickets 将已确认细节写入现有四张实施票；不重复设计访谈或请求拆分批准。

- 保持四个完整切片：冷请求与显式释放 → prompt 保存 → 命中恢复与后缀 → 完整验收。
  直接 blocker 保持 01 → 02 → 03 → 04，上游用户 NPU 验收并关闭后解锁下游。
- 实现目标明确为 Ascend NPU。Main 是独立进程，SR 协调者位于各侧 rank 0 scheduler，
  每 rank 有一个执行分片；网络只入队，有序 tick 推进 TP 一致动作。
- 阶段 0 的 alive 以整槽承载逻辑 page 接口，prefix 真正按 page 分配；HBM 页单独
  处理。私有副本、同 NUMA 放置与共享物理内容分开，后续共享目标继续保留。
- 01 必须实际预留 D 原生页／行，并接管全部旧自动释放；02 保证 P HBM 释放后
  仍能保存主 KV 与 Index K；03 验证双重恢复、两段就绪及 4032-token 半页接续。
- 同一双机入口从 01 开始交付常驻服务和自运行验收；02／03 扩展，04 在最终
  集成版本执行独立参考组、A／B／C／E、V1–V10 及拒绝／失败场景。
- 每次实现后即时更新[目标页 D](target-alignment.md)，同步 HTML，分别记录代码和
  用户 NPU 验收状态。四票只完成阶段 0，后续共享、远端 prefix、并发及 dense 仍在目标中。

本轮核对基线 `3883624810` 加当前工作区。四票均为 ready-for-agent／open，
全部验收项未勾选；只修改任务与进度文档，没有实现功能或运行新功能测试。
最新任务入口见[ticket-plan.md](ticket-plan.md)。

发布检查已通过：四票原有验收项及历史记录保留、依赖正确、本地引用有效，目标页
Markdown／HTML 内容一致；本轮未修改父规格或 AGENTS。检查结果记录在任务索引中。

## 2026-10-24：统一 token 页大小（用户明确决定）

DRAM 逻辑页与原生 HBM 页严格共享同一个可配置 `page_size=B`。
该约束覆盖 sparse、dense、P/D、alive、prefix、Index K 相关存储池。
仅为整数倍不算一致。本决定取代旧 sparse 固定 DRAM=64／HBM=128 约定。
旧轮次及 Comments 保留历史；现行正文、公式、验收以本决定为准。

- 哈希按 B 个 token 分块；目录页 q 覆盖 `[qB,(q+1)B)`。
- 阶段 0 输入满足 `B <= prompt_len <= P_max` 且 `prompt_len % B == 0`。
- prefix 命中上限 `floor((prompt_len-1)/B)*B`；dense 还须有对应可恢复状态。
- 整槽额度 `S_P/B`、`S_D/B`；池内逻辑页为 `slot*(S/B)+page_in_slot`。
- HBM 预留 `ceil((prompt_len+max_new_tokens)/B)`；实际 token 索引按原生映射构造。
- 主 KV 每页 `B*78*1152` 字节；Index K 每页 `B*21*256` 字节。
- sparse prefix 容量保持 5120 token，即 `5120/B` 页；副本数不扩大可缓存逻辑容量。
- dense 状态检查点和 D 跟踪间隔都为 B；后端必须能在所需边界捕获状态。
- OS 页、字节对齐、kernel tile/chunk、特征维（如 512/64）和 Graph padding 各按原义处理。

当前 GLM-5.2 mempool-min 默认且仅接受 B=128，这是本集成的支持范围，
不是所有 Ascend 后端能力的声明；其它值先验证对应模型/内核/池并扩展允许集合。
dense 本仓库尚未集成，Kimi-Linear 的实际支持范围和状态预算须在接入时验证。

B=128 时：A=4096 token=32 页；B 请求=5120 token=40 页；C 命中3968，
恢复31页、重算最后128 token。prefix 32→40→40→40，E 保持满池跳过保存。
P/D 整槽分别64/1页；native 预留33/41/33/33页。生成提前结束时，
已用页、实际尾页及未用预留仍须在 drain 后仅释放一次。

起点已核对为 `cryang/dev/mempool-min` / `af517b1a23`，已有未提交修改保留。
ticket01 仍 open，等待用户 NPU 验收；02–04 不解锁；CPU 结果不代表硬件通过。

## 2026-11-02：同步K1 page first决策（M17 / M58）

用户要求将设计侧2026-11-01定稿迁入本仓库；本轮只同步文档，不开始12或11实现。
以下两条原文摘自[设计侧discussion.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/discussion.md)，
与本文件此前的实施讨论分开保留；完整实施规格见[K1执行文本第16节](../k1/k1-tickets.md)。

| 编号 | 设计侧已确认原文 |
|---|---|
| M17 | 布局（R2-2）：阶段 0 可以沿用 layer-first；C28 的物理布局改造放到后续阶段，完整方案仍采用 page-first。理由修正：两种布局都能支持本地和远端复制，差别包括连续范围、分段数量和提交开销；暂缓修改是为了减少阶段 0 的改动，不能写成"本机复制完全没区别"或"只有远端复制才需要 page-first"。保留 layer-first 不等于已有 page 管理：阶段 0 可以用连续页区间适配现有 slot 存储，但要明确这是临时后端；逻辑页描述、分配接口和传输描述符仍需保留，不能假设已经支持任意散页分配，也不要让 main router 依赖 slot × S 的物理寻址公式。2026-11-01 补注（M58）：page first 提前到 K1 票 12，不再等阶段 1；阶段 0 保持 layer-first 的理由不变（历史）。 |
| M58 | K1 加票 12：page first 提前到 K1（2026-11-01，用户决定；票 12 的方案以设计侧为主，实施侧 2026-11-01 的现状与建议稿为事实基线）：实施侧关闭 07、08 后指出主存仍是 layer-first 排列（alive [层, 槽, 槽内 token, head, feature]，prefix 段 [层, 段内页, 页内 token, head, feature]），page first 原定阶段 1；用户决定在票 11 之前新增票 12（K1-05d），把五类主存区域（P / D alive 主 KV、P alive Index K、prefix 主 KV 与 Index K 的 8 段）改为 C32 的页 → 层 → token，HBM 原生布局不变。理由：票 11 要按整页块搬运，layer-first 下每页主 KV 78 段加 Index K 21 段，11 做完再改布局要重做搬运路径；M17 暂缓的理由（减少阶段 0 改动）只对阶段 0 成立。决定：① 五类区域一次全转，两类数据分区、同一逻辑页两个连续块（主 KV 11,501,568 B、Index K 688,128 B）；alive 物理页 = 槽 × (S ÷ 128) + 槽内页，prefix 物理页 = 段内页；地址 = 区域基址 + 物理页 × page_bytes + 层 × 128 × 行宽 + 页内 token × 行宽 + 行内偏移。② 只保留 page first，不设生产开关；布局类型、B、各维度进注册 / describe 与启动签名；重启建新池、不做在线迁移。③ 现有按 token 行索引的拷贝接口（单行 ≤ 32 KiB、单次跨度 ≤ 4 GiB，而 P 主 KV 区域约 10.97 GiB）先验证：默认按槽 / 段给基址使每次调用跨度 < 4 GiB、不改拷贝内核，不够再最小扩展为 64 位偏移，作为票内第一步的待验证项；不把整页伪装成一条 token 行。④ 票 12 只保证布局正确并提供整页块接口，仍用逐层索引拷贝、不加执行器线程；票 11 再接批量异步接口与执行器，并定义库完成与设备事件之间的完成桥接（设备事件不能证明库传输队列已完成；fill 完成进 prompt ready 汇总、admit 完成才 publish）。⑤ 清单按物理目标副本分别计：admit 每页 2 块、fill 每页 9 块（8 份主 KV + 1 份 Index K），32 页为 64 / 288 块（原 3168 / 20640 层片段）；旧的“N × 99”只对 layer-first 的 admit 成立。⑥ 验收沿用 08 批准的证据边界（源快照、load_back 后设备数据、写读记录；不新增直接主存读回；物理 NUMA 驻留与性能不在本票），新增几何 / 宽地址 / 独立 CPU oracle / 图重放 / 布局签名证据；最多 2 轮、复用服务启动；不出基线表（性能埋点在测试分支，08 也无可并列的表）、不承诺性能收益。依赖：12 依赖 08；11 改依赖 12；09 保留 06、08、11（经 11 传递依赖 12）；主线 07 → 08 → 12 → 11 → 09；K1 共 12 票。进度事实（实施侧 2026-11-01）：07、08 已关闭（08 两轮各 6 请求通过，五项副本数按最终布局注册 applied；07 严格答案报告仍 failed、冷热 26 token 一致，用户接受）；底层会话为 P / D 共 32 参与者，设备映射覆盖实际访问者，不是 owner-only；fill / load_back 用独立设备流但仍由 worker 有序路径提交，没有 11 的执行器线程；真实并发属阶段 3a、不在 09；旧阶段 0 的 02 文件仍 open、不阻塞。记录改动清单见 `k1-tickets.md` 第 18 节。 |

用户补充的协作边界：设计agent负责整体架构逻辑，不掌握Ascend/NPU具体实现。
实施侧以实际源码和验证决定接口、内核、地址及图捕获的具体落地；设计描述不作为硬件支持证据。
当前恢复入口见[K1项目记忆](../k1/memory.md)；12尚未实现或验收，用户确认12的NPU验收后才解锁11。
