# K2：sparse attention + KV cache offloading

**Status:** ready-for-agent

**State:** open

2026-11-03 按用户交接同步。K1 的 10 张功能票已验收关闭；04 并入 K2 14、
10 并入 K2 13，按本次用户指令关闭旧票。K1 现为 12 closed，其中 2 张是转归关闭，
不代表微测或遗留定位已完成。K2 13 张票均 open，本轮仅同步与源码分析。

## 执行依据与阅读顺序

1. [当前记忆](memory.md)：开工条件、当前代码与用户侧工作的区别。
2. [执行文本](k2-tickets.md)：原文快照，20 节（0–19），第 18 节是交付与关闭。
3. 下表对应票文件：做什么与验收；机制以执行文本对应节为准。
4. [四项实施答复](handoff-check-20261103.md)：13 的统计口径建议、14/18 的源码事实、25 的完成路径建议。
5. [K1 执行文本](../k1/k1-tickets.md)第 1 节与第 3 节第 4–11 条继续适用。
6. [路线与测量](../paperwriting-sparse/13-roadmap-and-measurements.md)第 4、5、7 节、
   [命中页放置](../paperwriting-sparse/14-hit-page-placement.md)第 4、8 节、
   [评测](../paperwriting-sparse/11-evaluation.md)第 4 节。

源文件来自 `/Users/hibikid/Documents/syncfile/sglang-kv-explainer/`。
[同步清单](sync-manifest.json)记录 33 份源文件路径和 SHA-256。
执行文本、论文目录和两份 M60 设计源快照逐字节保存。
票文件仅规范化 Status、补 State、依赖链接与本地 Comments；正文及勾选项不改写。
源稿内沿用设计仓库的相对路径和旧文件位置；本地导航使用本索引。
`sources/mempool-min-demo/` 保留 discussion / walkthrough 原稿，避免覆盖实施侧历史讨论。

用户本次指令优先，其次是本页记录的实施事实与源稿冲突说明，再查执行文本。
源码核对基点为 `dc31b37cbe`；工作区已有未提交资料保留。

## 票据、执行节与开工条件

| 票 | 执行节 | 依赖 / 当前状态 |
|---|---|---|
| [13 观测模式](issues/13-k2-m1-observability-mode.md) | 5 | 可立即开始；尚未实施 |
| [14 带宽刻画](issues/14-k2-m2-bandwidth-characterization.md) | 6 | 可立即开始，不经服务；远端写一项先交付 |
| [18 页粒度 alive](issues/18-k2-s0-page-granular-alive.md) | 10 | 13 验收关闭；接用户 64 位单基址 kernel，不等多基址 |
| [25 双写](issues/25-k2-p1-dual-write.md) | 17 | 18；14 远端写结果支持层间隐藏；跨区域多基址 kernel 可用 |
| [20 多 chunk](issues/20-k2-c4-chunked-prefill.md) | 12 | 25；默认 8192 token，页对齐 |
| [15 并发](issues/15-k2-c1-concurrency-minimal.md) | 7 | 20；reserve 不足进 FIFO，不拒绝 |
| [16 多 attn group](issues/16-k2-c3-multi-attn-group.md) | 8 | 15；默认每组 scheduler 内一个 SR 协调者 |
| [17 带宽准入](issues/17-k2-s1-bandwidth-accounting-admission.md) | 9 | 16、13、14；miss 估计先配置常数，再接在线统计 |
| [21 跨层预取](issues/21-k2-w1-cross-layer-prefetch.md) | 13 | 13；先验证共享层选择一致；第一步自跑同机 HiSparse 对照 |
| [22 热缓存与读合并](issues/22-k2-w2-hot-cache-and-coalescing.md) | 14 | 13；对齐判据用 21 归档数据；真实跨请求合并验证需 15 的并发能力 |
| [19 热页复制](issues/19-k2-s2-hot-page-replication.md) | 11 | 17、18；等第 1/2 组数据与 25 结果，用户定开票判据 |
| [23 alive hit 引用](issues/23-k2-c2-alive-hit-minimal.md) | 15 | 25、15、18；按交接等第 1/2 组数据与 25 结果再开 |
| [24 评测台](issues/24-k2-e-evaluation-harness.md) | 16 | 15（台子）、17（claim 图）；SLO 上限待用户给定 |

主线：13 → 18 → 25 → 20 → 15 → 16 → 17 → 24。
14 可与 13 并行；21/22 可在 13 后开展。ready-for-agent 表示可规划，不覆盖上述依赖。

## 共同边界

- 不出旧基线表，不逐票重报三项遗留；遗留归入 13 的观测模式。JSON lines 加汇总表，默认关。
- 保持 prefix 保存、部分/完全命中、释放复用、入口拒绝、fail-stop、答案逐位一致。
  K1 已接受的证据限制保留，不追认历史严格报告为 passed。
- 策略票 17/19/25 与 21/22 策略部分带开关，关闭回到直接前置版本行为；关闭票时记录默认值。
- 不设性能门槛。变差记录原因，未解释项单列，不自动优化。
  21/22 仅按同机、同 B、单请求的每 token 暴露 miss IO 对齐 HiSparse。
- 最多 2 轮，第 1 轮预热，报告实际 n。14 每档 3 次是轮内采样，不是 3 轮服务重启。
- 每票单 commit、Standards/Spec 两轴评审、自运行入口；原始样本、日志、拓扑与版本归档。
  NPU 由用户运行，日志 `/home/cryang/log/`；用户确认后关闭并解锁依赖。
- 证据沿用源快照、正常恢复后的设备内容与写读记录；不新增直接主存读回。
  物理 NUMA 驻留仅 14 小程序抽样；16 核对注册/映射/账本，不扩成服务驻留探针。
- 冻结范围只开放双写、16 的副本重配 C62/C67/C68；只读跨机 prefix 留 16 后另定。
  dense、远端 prefix 搬池、动态 grant/retract/resume/evict、decode admit、混合页、D3 均不进入本批。
  本次不修改完整 `mempool-design/*.md`。

## 同步核对：源稿滞后与待澄清口径

| 项 | 本地采用的解释 / 实施前需确认的范围 |
|---|---|
| K1 04/10 的关闭时间 | 源稿仍写随 14/13 关闭；本次用户明确“随之关闭”，本地现在按转归关闭，保留未勾选验收项 |
| 13 的清理基点 | `dc31b37cbe` 已删除 P/D 临时探针、人工延迟、搬运计时/poll；有用户单配置回传。13 核查残留并引用既有删除清单，不重复记功；源快照等保留证据须迁入模式，不能丢失正确性验收 |
| 13 的缓存事实 | 本地 `sparse_context_len` 取模型 top-k，slot map 每步重建；源稿“4096/LRU”不代表本地已实现。13 记录实际容量和策略，接用户三档日志；独立缓存容量与策略修改留22，详见[实施方案](ticket13-implementation-plan.md) |
| 64 位 kernel | 用户侧已完成；本机旁侧 kernel 与 indexed_copy 仍为 uint32 窗口。18 接入时记录交付版本并撤掉窗口，不把它当本地已实现 |
| 18 的多基址表述 | 第 0/10 节部分概述仍写多基址；按同节风险说明与用户交接，18 用固定 P/D 两源分别单基址 64 位 gather；25 才依赖多基址 |
| 15 的“单 chunk 不变” | 与 20 → 15 的依赖冲突；按用户指定顺序，15 保持 20 已有多 chunk 行为 |
| 25 的“decode 只有 D 本地” | 仅新算 prompt 页与生成页成立；命中页引用 P prefix 正本，仍记 P 远端 miss。B/C 不能要求远端字节为零 |
| 25 的写字节等式 | 仅对实际新算且有候选页的主 KV 成立；命中页不双写，prefix 放不下时本机候选写为零。Index K 保持原生 P HBM→D HBM 传输，另写 P prefix；源稿“主 KV 与 Index K 分别相等”不能解释为新增 D alive Index K 区域 |
| 25 关 = K1 路径 | 恢复 K1 的 P alive 放置、远端读、op1/op3；保留已经验收的 18 页分配与后续 chunk 能力，不回滚整槽后端 |
| 25 两区域“不分配” | 不给请求分配 P alive 页；区域映射/注册/启动签名仍保留，本票不宣称减少 BM 预留物理容量 |
| SLO 的使用 | 24 上限待用户给定；17 的 SLO goodput 验收也使用同一配置，不自行选数，不影响当前 13/14 |

上述冲突保留原稿，并在本地单列，供设计侧补注；本次没有写回 syncfile。
13 的分解口径和 25 的完成路径是本次源码核对后的建议，尚不宣称已获确认或硬件验证。

## Comments

### 2026-11-03：首次同步

已导入执行文本、十三张票、论文方向资料，并同步 K1 当前状态与目标页 D。
核对 33 份源文件、票据 13–25、20 节结构、依赖和本地保留记录。
功能代码零改动；未运行 CPU/NPU 功能测试；没有关闭任何 K2 票。
