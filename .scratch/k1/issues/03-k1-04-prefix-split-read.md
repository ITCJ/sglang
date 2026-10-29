# 03: K1-04 prefix split_read（16 份布局）

**What to build:** B、C 命中时，load_back 不再由每个 rank 读整段，而是每个 rank 只从自己那份 prefix 读分到的页进 HBM 暂存区，按层 all_gather 成全集，再按页表散开到各自的 HBM 页，主 KV 按原生映射拆回两个缓冲，Index K 同样分法；输出与参考一致。读者到页的分配是一个可配置的函数，默认轮转，页数不整除时补齐；集合通信只在有序调度 tick 里发起；协调者在所有 rank 散开完之后才对 main router ack。副本数不变，op1、op3 不变。细节见 k1-tickets.md 第 7 节。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

**State:** open

- [ ] B、C 命中用例通过 V1 / V3 / V6 / V7
- [ ] V2 改为 all_gather 后的全集与本 rank 私有副本逐 token 一致，主 KV 和 Index K 都比
- [ ] 每 rank 读取字节计数：每页恰好被一个 rank 读一次，总和等于命中页字节
- [ ] op2 的 ack 时刻晚于最后一个 rank 散开完成
- [ ] 把分配函数换成另一种（例如连续块）结果不变
- [ ] 基线表并列，重点看 T_lb_rank 与 op2；不设门槛，变差要解释

## Comments

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
