# 06: K1-03b D alive 主 KV 共享与每步顺序

**What to build:** D 侧打开 share=1：组 {2k, 2k+1} 共用一份 alive 主 KV，每层算完两个成员各写新 token 这一层的一半特征维（可配为按层切）；下一步任一成员 swap_in 读到对方写的那一半是正确的。顺序靠写回排在本 rank 本层第一次全员集合通信之前来保证（同流天然成立，单独流则 compute stream 等写回 event），不加跨进程消息；T_step 超出轮间波动再换“本步最后一次全员集合通信前等齐”的放宽版本。D 侧从 16 份变 8 份；不依赖 P 侧共享。细节见 k1-tickets.md 第 10 节。

**Blocked by:** [01（K1-02a）](01-k1-02a-explicit-numa-mapping.md)、[02（K1-02b）](02-k1-02b-shared-pool-adapter-share0.md)

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V4 加“读对方 rank 写的 token 的最早一步”（第 1 步读位置 prompt_len）
- [ ] 核对并记录：现在写回走哪条流、是 memcpy 还是能入图的写 kernel；所依赖的集合通信是全员的，不是 broadcast
- [ ] 哨兵每步校验通过：debug 模式下第 t+1 步开始前本 rank 从 DRAM 读回 token t 整行，与自己 HBM 里的 token t 一致
- [ ] 负测试通过：在对方写回流里注入延迟，关掉等待失败、打开等待通过
- [ ] Graph replay 模式下写回和 event 在捕获范围内，校验通过
- [ ] 写覆盖：两个写者范围并集等于整行且不相交；注册快照 R_am(D) = 8；D 每 NUMA 常驻内存等于 1 份副本；free_ack 在两个成员都停之后
- [ ] 基线表并列，重点看 T_step p50 / p99、T_finish、T_free

## Comments

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
