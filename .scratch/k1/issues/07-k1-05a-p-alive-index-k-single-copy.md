# 07: K1-05a P alive Index K 单份

**What to build:** P alive 的 Index K DRAM 区域只留 1 份，放 NUMA 0，owner 是 rank 0，16 个进程 CPU 映射、只有 rank 0 做 GPU 注册；write_through 的 Index K 全部由 rank 0 写（按页分给 rank 1 留作配置项），op1 的 Index K 部分由 rank 0 的分片填，op3 时每个 rank 从这块区域把 Index K 拷进自己那份 prefix，ready_tokens 的 Index K 项改为这块区域的完成。顺带第一次做出 16 参与者会话（1 个 owner 加 15 个映射者）。prefix 仍 16 份；D 不读它，prompt 的 Index K 仍从 HBM 推送。细节见 k1-tickets.md 第 11 节。

**Blocked by:** [01（K1-02a）](01-k1-02a-explicit-numa-mapping.md)、[02（K1-02b）](02-k1-02b-shared-pool-adapter-share0.md)

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V2 加 Index K 内容核对：admit 后每 rank 那份 prefix 的 Index K 与区域一致
- [ ] 注册快照 R_ai = 1，区域 applied，页驻留在 NUMA 0
- [ ] 字节计数证明只有 rank 0 写这块区域
- [ ] 16 参与者会话的创建与关闭干净（正常结束与 fail-stop）
- [ ] 基线表并列，重点看 T_wt_rank、T_ready、op1、op3

## Comments

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
