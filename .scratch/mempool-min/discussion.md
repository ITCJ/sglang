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
