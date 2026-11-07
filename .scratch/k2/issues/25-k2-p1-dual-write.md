# 25: K2-P1 双写（先试）

**What to build:** 新算的 prompt 页在 prefill 期间双写到 D 组 alive 与 P prefix 候选页，admit 只做 publish，D 的 decode 读两处（新算的 prompt 页与生成页读 D 本地；命中页引用 prefix 正本就地读，是远端读；2026-11-06 改，原写"只读本地"），生成页只写 D alive；带开关，关 = K1 路径。route 时足额 reserve 加 prefix 候选页一项（放不下退成"不存"、只写 D）；P write_through 每层两目标（远端 D 组区域的设备映射、本机交错分配的候选页），远端写完成以真实完成为准，ready_tokens 只数完成的；D 上每份副本由对应的一对 P rank 跨机写、按页各写一半，每个 NUMA 只收一份跨机数据（TP 16 是 8 份，没有 D 本机复制；2026-11-06）；Index K 只写 prefix 的 Index K 候选页（D 侧走原生 send，没有远端写目标）；P alive 主 KV 与 P alive Index K 两区域开关开时不分配（仍在映射与注册里、启动签名不变）；命中页通过页表引用 prefix 正本（多基址 kernel 加 prefix 段为源），不再 op1 fill 到 alive；admit 请求结束时只 publish（哈希重检查，重复则丢弃候选页并归还），op3 的拷贝不再发生，释放顺序仍按 M30；D 读路径：新算 prompt 页的源从 P alive 改为 D alive，命中页的源改为 prefix 正本。publish 语义与时机（请求结束；提前是票 23）、D 的三条件门控、Index K 的原生传输、热缓存、fail-stop 不变。开工条件（2026-11-06 改）：票 26 已确认 kernel 路径远端写的可见性契约、并对能否藏在一层计算后面有结论；票 14 的 SDK 路径结果只作对照。流程表见 ../paperwriting-sparse/14-hit-page-placement.md 第 8 节；细节见 k2-tickets.md 第 17 节。

**Blocked by:** [26](26-unidex-multibase-backend.md)（K2-S0b；2026-11-06 改，原为 [18](18-k2-s0-page-granular-alive.md)、[14](14-k2-m2-bandwidth-characterization.md) 的远端写一项、多基址 kernel 可用）。Blocks [20](20-k2-c4-chunked-prefill.md)、[23](23-k2-c2-alive-hit-minimal.md)。

**Status:** ready-for-agent

**State:** open

**2026-11-07实现状态：** 本地实现、CPU关键验证及两轴静态评审完成；
用户要求不安排双机on，真实kernel/远端可见性/Graph/计算重叠均未验证。
[交付与源码清单](../../../ascend-mempool-test/TICKET25.md)。下方验收项不提前勾选。

**2026-11-07依赖更新：** 26已按四来源物化范围经用户确认关闭，本票可进入实施。原方案中的未来基址集合、页表路由及双写任务准备已在26回退，不能假定现成；本票承接这些实现及远端写可见性、计算重叠验证。历史“26未关闭/不解锁”记录不再代表当前状态。

- [ ] A / B / C / E 与 V1–V10 在开关两态都过，答案逐位一致
- [ ] 开：主 KV 远端写字节 = R × 新算 prompt 主 KV 字节（TP 16 时 R = 8），候选页写字节 = 1 × 新算 prompt 主 KV 字节；Index K 候选页写字节 = 新算 prompt Index K 字节，D 侧走原生 send（2026-11-06 改）
- [ ] 开：decode 按源计数：新算 prompt 页与生成页只有 D 本地，命中页只有 prefix 正本（远端），P alive 为 0（冷请求 A、E 只有 D 本地）；op1 / op3 零拷贝（2026-11-06 改）
- [ ] 开：请求结束后 prefix 命中可用（B、C 用例）；prefix 放不下时退成只写 D 且后续命中为 0
- [ ] 负测试：远端写失败 → fail-stop；publish 时哈希重复 → 候选页归还
- [ ] 重启与清理；观测模式下 TTFT 分解与 T_step 对照票 13（不设门槛）；单 commit；两轴评审

## Comments

### 2026-11-06：26成为直接前置

按计划侧确认，顺序26 → 25 → 20。26建全基址集合，至少验证8个prefix段、D alive和两HBM源的后续形状。
本票更新页表路由和写任务，不再新增25所需Graph捕获地址集合；多DRAM一次读是必需项，HBM融合可暂缓。
双写采用同一HBM源行的两条目标任务，按目标分别记完成；共享完成event不能伪装成独立完成时刻。
kernel远端写须在写者确认后由D立即读回比对，并有同负载一层计算隐藏证据。
14旧SDK数据仅供对照，旧正文的开工条件由本条和[26方案](../ticket26-plan.md)细化。
本票未实施；原正文的Index K和命中页读来源歧义继续按spec解释。

### 2026-11-03：同步设计侧交接

源稿 Status 注记：ready-for-agent（2026-11-03 加票；用户决定先试）。
正文与验收项按源稿导入；State=open。代码未实施，CPU/NPU 验收未运行。
当前可开工项、数据门槛和源稿冲突按[本地 spec](../spec.md)的同步核对记录执行。

### 2026-11-05：14实测已接受

用户接受001 remote-write结果并关闭14，解除本票对14的票据依赖。
见[14实测记录](../verification/ticket14-001/results.md)。8K双写首组完成中位数0.488 ms。
报告未提供同机计算窗口，不能据此声称层间隐藏已证明；多基址kernel可用性也未由本次验收确认。
本票实现与自身NPU验收尚未开始。


### 2026-11-06：同步计划侧更新稿

已同步[source原稿](../sources/mempool-min-demo/k2/issues/25-k2-p1-dual-write.md)中的范围、依赖与验收条目。
本地Status/State、已有验收勾选、关闭事实和历史Comments保留；源稿的closed状态归入State，不作为triage label。
本次仅同步文档；没有实施功能、执行实机测试或新增关闭结论。

### 2026-11-07：承接26收窄后剩余双写准备

用户确认26关闭。双写所需Graph前地址表、页表/任务准备及远端写可见性、计算重叠结论在本票完成；既有微测仅为历史参考，不能冒充当前生产路径已验证。本票仍open，本轮未开始实施。

### 2026-11-07：Ticket25 本地实现交付，NPU未验收

用户调用 implement 授权当前分支实施。双写默认关闭；完成请求开始的D/candidate预留、
P零页绑定、直接prefix读取、主KV两目标写、候选Index K写、完成计数和无拷贝publish。
通用主线源码无修改，沿用原清理路径。源码清单与命令见
[Ticket25交付](../../../ascend-mempool-test/TICKET25.md)。

用户缩小新增测试到router/SR服务边界，并明确“双机的on不要作”。
新增3项CPU通过；完整集合1轮及受限方法第2轮后仍有9个未通过方法，
其中7个既有基线问题、1个清理权限问题、1个旧性能脚本的manager基线一致性拒绝。
不重复完整集合，不移除旧性能脚本的保护检查。详见
[验证记录](../verification/ticket25-local/checks.json)。

Standards 3项建议修复，Spec 4项问题修复，静态剩余0。
双机off入口已文档化，未实际执行；不安排on。真实kernel、远端可见性、Graph、
计算重叠均未验证。State保持open，不勾选NPU验收，不解锁20/23。

- 2026-11-07用户回传off-002两轮通过；明确授权双写on、observe off。单服务功能smoke已补齐，待实机，票仍open。详见TICKET25.md。
