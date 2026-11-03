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

### 2026-11-03：31 首次 Graph 验收反馈与 trace 解析修复

用户运行 `k2-ticket13-001`；页计数和快照通过，trace gate 失败，模型服务未启动。
原始事件证明设备 lane 为 `Stream 44`，带 `Physic Stream Id` / `Task Id`，时间戳为字符串。
本次修复设备识别、Decimal 时间转换及 host anchor；新增离线重分析和详细 gate 证据。
15项观测测试通过；同一 unittest 最多两轮，未重复完整套件。
[修复范围、验证与命令](../ticket13-trace-fix.md)。

用户原始图内区间横跨两次 replay；修复解析后仍应失败，不能放宽 gate 或用主机时间代替。
图内计时问题尚未解决；State=open，18/21/22不解锁。

### 2026-11-03：CANN9.0 原始日志配对修复

用户上传原始profiler包，并明确保留CANN9.0.0，只做日志补丁。
原始index5–8各四个打点完整；旧CANN解析将相同index的首尾合并。
日志分析新增兼容恢复，按主机确认的start/end标签、真实设备端点、频率和捕获流映射重算。
已上传实测包离线检查passed，两轮各含四个阶段；18项CPU测试通过。
[交付、源码与复核命令](../ticket13-cann90-log-fix.md)。
没有改变运行环境或采集行为；完整模型NPU验收仍待用户执行，State=open，依赖不解锁。

### 2026-11-03：用户确认31日志复核passed

用户在31使用日志补丁重分析k2-ticket13-001，返回status=passed，
forward:1/2各1条，两轮missing=[]，timing_source=CANN9.0_raw_endpoints。
这是既有设备采集的trace复核；本次analyze-only不重新检查页快照。
原始首次采集已通过页计数/快照检查。完整模型、双侧off/on与字节/开销验收仍待执行。
下一次双机完整入口使用新run-id=k2-ticket13-002；ticket13保持open。

### 2026-11-04：fork沿用K1-07已接受的模型精度边界

用户003新Graph/页快照passed，原生参考A/C一致；新增fork输出围栏中的12，26 tokens正常EOS。
用户再次明确该17→12已排查为量化模型精度问题；此前K1-07冷/热完整26 IDs一致且已接受。
本轮错误是把同一旧题重新作为严格逻辑答案阻断条件。
修复仅调整该fork：保留answer failed，目标/本轮正式参考完整IDs须一致；原6例和off/on门槛不变。
13项定向CPU测试通过；[交付与004命令](../ticket13-fork-baseline-fix.md)。
不修改题面、期望答案、JSON解析或模型；新NPU复验待用户运行，ticket保持open。

**2026-11-04 空请求观测修复与暂时验收约定。**

004 的 b128-sd128-off 完整通过；on 在 decode warmup 因空 rows 默认 float 索引失败。
修复 runtime 快照索引为 int64；空 replay 回归先复现后通过，观测/runtime 32 项 CPU 测试通过。
新增 --geometry b64-sd128 --observe on，只执行指定服务组合，保留 Graph gate 和该组完整功能检查。
用户明确约定：下一轮该组通过即暂时接受13，然后推进18；其余测试由用户自行安排，不提醒。
当前修复已实现，所选 NPU 组尚待结果；未提前宣布13通过或开始18实现。

**2026-11-04 005 离线容量重放修复。**

005 Graph 通过，restart 后离线 capacity_check 使用 observe=True 创建 MainRouter，触发服务包导入错误。
仅为离线准入重放复制 observe=False 配置；真实服务继续 observe on，容量不足及账本检查保留。
回归先复现导入错误；修复后该回归与原容量重放检查通过。
整份 session 测试运行16项，存在7个旧端口断言失败及1个沙箱 bind 权限错误，未宣称全通过。
下一次仍只运行 b64-sd128-on，使用新 run-id 006。暂时验收及随后推进18的约定不变。

**2026-11-04 用户调整：默认关闭 trace。**

006 在同步 profiler 解析期间停止 SR 心跳，600秒后 fail-stop；单个未完成 trace 超过1.2GB。
用户要求停止自动 trace 采集，后续按需手动采少量 step。
新增 observe_trace=False；普通 observe on 保留 JSONL、页统计、字节及生命周期验证。
默认跳过 Graph trace gate；报告标记 device_timing=not-collected，未将设备计时视为已通过。
撤回未提交的自动离线导出方案。48项CPU测试通过；Standards/Spec评审无阻断项。
下一轮007仍为 b64-sd128-on，trace关闭；此组通过即按用户约定暂时接受13并推进18。
