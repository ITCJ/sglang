# 08: K1-05b prefix 单份：8 段、numa interleave、按段读者、并行 admit、直接 fill

**What to build:** prefix 主 KV 与 Index K 各只留 1 份，池切成 8 个 NUMA 段，段 k 的 owner 是 rank 2k；admit 分配连续页时在段间轮转（起始段按请求轮转，段空则取最空的），8 个 owner 并行各写本段的页、源是本 NUMA 的 alive 副本，全程本地（单 rank 版留作配置项）；fill 由 8 个组各自从全部段直接读进自己的副本；load_back 的读者分配切换为按段，组 k 读段 k、组内两成员平分，每 NUMA 两个读者；描述符编码段号与偏移，main router 仍只看到每页 1 个描述符；规划器补齐 8 段并定稿 8 / 8 / 1 / 1 / 1 映射。op1 / op3 沿用现有索引拷贝（换批量异步接口是票 11 的事），选页、地址解析与拷贝提交分开，地址由持有映射的分片解析；CPU 映射范围覆盖执行分片实际访问的区域（fill 读全部段，owner 读本 NUMA 的 alive 副本与 NUMA 0 的 Index K 区域），不预设只能映射本段，增加映射不等于增加物理副本。B、C 命中输出正确。细节见 k1-tickets.md 第 12 节。

**Blocked by:** [05（K1-03a P alive 主 KV 共享）](05-k1-03a-p-alive-main-kv-sharing.md)、[03（K1-04 prefix split_read（16 份布局））](03-k1-04-prefix-split-read.md)、[07（K1-05a P alive Index K 单份）](07-k1-05a-p-alive-index-k-single-copy.md)

**Status:** needs-info

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V2 改为冷算 A 时按页另存校验摘要，op1 后 8 份副本、op2 后 HBM 页都与摘要一致
- [ ] 注册快照 = 映射文件（applied），R_pm = R_pi = 1；页驻留逐段核对；每 NUMA 常驻内存 = 1 份 alive 副本 + 本段 prefix（NUMA 0 另加 Index K 区域）
- [ ] 分配日志：同一请求的连续页落在轮转的段上，起始段随请求轮转
- [ ] 读字节计数：load_back 每段读取量相等（补齐后）；fill 每段被 8 个组各读八分之一；admit 的写全部落本段
- [ ] admit 每页只有一个写者；账本 prefix 按 1 份、alive 按 8 份记，V6 等式用注册值重算
- [ ] 共享段在首次触碰前绑定到目标 NUMA；GPU 只注册本段，注册范围和耗时记进快照
- [ ] 选页、地址解析、拷贝提交三层分开；CPU 映射范围覆盖分片实际访问的区域，快照里映射者数与物理副本数分开记
- [ ] 基线表并列，重点看 op1、op2、op3、T_lb_rank；不设门槛，变差要解释

## Comments


### 2026-10-31：05依赖已满足

用户确认05的NPU002精简功能验收；03/05均closed。
本票仍等待07自身验收，State保持open，本次不开始实施。

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。

### 2026-11-01：07验收关闭，前置全部满足

用户确认07功能验收；03/05/07均closed，本票已解锁、尚未实施，State仍open。
07的接受范围和未验证项见[确认记录](../verification/ticket07-cold-20261101/acceptance.md)。

### 2026-11-01：按 syncfile 最新票据完成实施规划

用户要求本轮回顾进度、预先规划，compact 后再由 xhigh 实施。
已直接核对 syncfile 的 ticket08 及执行文本第12节，功能与验收正文和本地同步稿一致。
[执行计划](../ticket08-implementation-plan.md)记录分段布局、交错分配、三条拷贝路径、
单位置描述符、真实源校验、测试与分步完成条件；另有[总览](../ticket08-plan-overview.html)。

计划复用07的32参与者会话，当前索引拷贝保留到11；功能源码预计仅涉及Ascend/NPU。
用户既有的不新增BM直接读回、性能留测试分支和默认退出约定继续适用。
本轮没有修改功能或运行新CPU/NPU验收；08保持open，11仍等待08自身验收。

### 2026-11-01：实现与CPU验证完成，等待用户NPU验收

已实现八段单份prefix、交错分配、owner/single_rank admit、按段load_back、直接fill及Main单位置发布。
32参与者会话保持；P follower零贡献，源摘要按实际主KV/Index K写者关联。
功能改动全部位于Ascend/NPU，未改变默认退出、未新增请求级BM读回或性能代码。

完整CPU一次运行532项；21个方法受本机沙箱限制，单独补跑后全部覆盖通过。
24文件静态检查、18模块strict mypy通过；Standards/Spec双轴评审均0项发现。
[交付说明](../delivery-ticket08.md)列出最终源码阅读顺序、[验证记录](../verification/ticket08-cpu-20261101/README.md)与用户入口。

NPU未运行，原验收清单保持未勾选，State=open；用户确认后才能关闭并解锁11。
默认入口是两轮六例精简功能验收，NUMA物理驻留、性能、直接fill DRAM比较等缺项单列。
