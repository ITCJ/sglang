# 08: K1-05b prefix 单份：8 段、numa interleave、按段读者、并行 admit、直接 fill

**What to build:** prefix 主 KV 与 Index K 各只留 1 份，池切成 8 个 NUMA 段，段 k 的 owner 是 rank 2k；admit 分配连续页时在段间轮转（起始段按请求轮转，段空则取最空的），8 个 owner 并行各写本段的页、源是本 NUMA 的 alive 副本，全程本地（单 rank 版留作配置项）；fill 由 8 个组各自从全部段直接读进自己的副本；load_back 的读者分配切换为按段，组 k 读段 k、组内两成员平分，每 NUMA 两个读者；描述符编码段号与偏移，main router 仍只看到每页 1 个描述符；规划器补齐 8 段并定稿 8 / 8 / 1 / 1 / 1 映射。B、C 命中输出正确。细节见 k1-tickets.md 第 12 节。

**Blocked by:** [05（K1-03a）](05-k1-03a-p-alive-main-kv-sharing.md)、[03（K1-04）](03-k1-04-prefix-split-read.md)、[07（K1-05a）](07-k1-05a-p-alive-index-k-single-copy.md)

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V2 改为冷算 A 时按页另存校验摘要，op1 后 8 份副本、op2 后 HBM 页都与摘要一致
- [ ] 注册快照 = 映射文件（applied），R_pm = R_pi = 1；页驻留逐段核对；每 NUMA 常驻内存 = 1 份 alive 副本 + 本段 prefix（NUMA 0 另加 Index K 区域）
- [ ] 分配日志：同一请求的连续页落在轮转的段上，起始段随请求轮转
- [ ] 读字节计数：load_back 每段读取量相等（补齐后）；fill 每段被 8 个组各读八分之一；admit 的写全部落本段
- [ ] admit 每页只有一个写者；账本 prefix 按 1 份、alive 按 8 份记，V6 等式用注册值重算
- [ ] 共享段在首次触碰前绑定到目标 NUMA；GPU 只注册本段，注册范围和耗时记进快照
- [ ] 基线表并列，重点看 op1、op2、op3、T_lb_rank；不设门槛，变差要解释

## Comments

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
