# 13: K2-M1 观测模式（行为不变）

**What to build:** 验收入口的一个子模式，开关控制、默认关：每步（每请求）记 T_step 的分解（indexer、resolve / 热缓存管理、miss IO 按源 P 远端 / D 本地 / 副本区、控制 tick 等待、集合通信等待、attention 与 MoE 计算）、每层命中数 / 缺失数 / 缺失字节、选中 token 折成页号的页集合（每页记命中 / 缺失与缺失来源，带请求的 (机器, attn group 或 NUMA)）、producer 层之后各共享层的选择是否与 producer 层相同；每 op（op1 / op2 / op3 / write_through / 远端写）的提交、库完成、ack、publish、释放时间戳与按分片按源的字节计数；每请求的 TTFT 分解、T_tail、T_free、步数与停止边界。附离线脚本：每层缺失率随 B（吃用户并行跑的 B = k / 2k / 4k 日志）、页访问集中度、跨步重合、fork 兄弟重合（两条共享 prompt 的请求先后跑，共享段上页集合的 Jaccard）。同时清理工程里残留的验收探针、人工延迟、证据输出逻辑，并入票 10 的三项遗留（A 比 E 多出时间的归属、C 的 fill 长尾、128 对 127 步）。细节见 k2-tickets.md 第 5 节。

**Blocked by:** None (can start immediately). Blocks [18](18-k2-s0-page-granular-alive.md)、[21](21-k2-w1-cross-layer-prefetch.md)、[22](22-k2-w2-hot-cache-and-coalescing.md)；[17](17-k2-s1-bandwidth-accounting-admission.md)、[19](19-k2-s2-hot-page-replication.md)、[24](24-k2-e-evaluation-harness.md) 用它的数据。

**Status:** needs-info

**State:** open

- [ ] 开关关：票 09 的入口全过，无额外开销；开关开：同样全过，自身开销记进报告
- [ ] 分解之和与 T_step 的差可解释，未覆盖的段写明；字节计数与票 08 / 09 相同（B 用例主 KV load_back 368,050,176 B、fill 2,944,401,408 B、Index K 各 22,020,096 B）
- [ ] 页集合经离线脚本重算的缺失率与在线计数一致；四个离线脚本各有一次真实日志的输出；页集合每页带命中 / 缺失、缺失来源与读者的 (机器, 组)，用户并行跑的第 2 组日志同格式
- [ ] 三项遗留各有归属（四处打点、按分片的 fill 记录、停止边界口径），或改写为另一张票并注明
- [ ] 探针、人工延迟、证据输出逻辑的删除清单与 grep 结果随票交付
- [ ] 报告格式为 JSON lines 加汇总表；单 commit；Standards / Spec 两轴评审

## Comments

### 2026-11-03：同步设计侧交接

源稿 Status 注记：ready-for-agent（2026-11-03 加票）。
正文与验收项按源稿导入；State=open。代码未实施，CPU/NPU 验收未运行。
当前可开工项、数据门槛和源稿冲突按[本地 spec](../spec.md)的同步核对记录执行。

### 2026-11-03：实施前规划

按用户要求回顾代码约定，并核对当前实现与历史可复用代码。
[详细方案](../ticket13-implementation-plan.md)记录接入点、数据与计时口径、分步完成判据及验收编排。
本地缓存实际为 top-k 容量与逐步重建 slot map；方案按真实策略记录，策略修改留22。
Graph逐层计时先验 replay/task 关联；Main感知TTFT为待用户反馈的推荐口径。
通用主线预计仅增加 scheduler 结果完成通知，必要性与行为影响已在实施前说明。
本轮没有功能实现、CPU/NPU功能测试或commit；验收项不勾选，State保持open。

### 2026-11-03：实现交付，等待用户执行 NPU 验收

用户通过 `$implement` 授权按方案实施。默认关闭的JSONL、图内页计数与独立完成快照、
设备MSTX、Main/SR/SDK链路、TTFT/停止边界、四项分析及票09四组开关入口已接入。
正常阶段复用tick导出，保持原父进程退出路径。通用主线仅已提前说明的scheduler结果通知。

[交付与每机命令](../ticket13-delivery.md)；[HTML](../ticket13-delivery.html)；
[检查记录](../verification/ticket13-20261103/checks.json)；
[两轴评审](../verification/ticket13-20261103/review.md)；
[残留核查](../verification/ticket13-20261103/cleanup-audit.md)。

一次完整CPU563方法，受影响方法第二轮23项全过；最终567不同方法561通过，
6项为既有基线失败。19文件静态与16源严格类型、双侧describe通过；两轴复核0未解决。
同一测试不超过两轮，没有重复运行完整套件。

未运行NPU。Graph重放、真实逐层/HCCL计时、字节、开销、四项真实日志输出及用户三档缓存
均等待确认。State=open，验收项不勾选，18/21/22不解锁。
