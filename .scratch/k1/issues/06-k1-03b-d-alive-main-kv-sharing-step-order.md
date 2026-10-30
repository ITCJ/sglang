# 06: K1-03b D alive 主 KV 共享与每步顺序

**What to build:** D 侧打开 share=1：组 {2k, 2k+1} 共用一份 alive 主 KV，每层算完两个成员各写新 token 这一层的一半特征维（可配为按层切）；下一步任一成员 swap_in 读到对方写的那一半是正确的。顺序靠写回排在本 rank 本层第一次全员集合通信之前来保证（同流天然成立，单独流则 compute stream 等写回 event），不加跨进程消息；T_step 超出轮间波动再换“本步最后一次全员集合通信前等齐”的放宽版本。D 侧从 16 份变 8 份；不依赖 P 侧共享。细节见 k1-tickets.md 第 10 节。

**Blocked by:** [01（K1-02a）](01-k1-02a-explicit-numa-mapping.md)、[02（K1-02b）](02-k1-02b-shared-pool-adapter-share0.md)（均已用户验收关闭，依赖已满足）

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V4 加“读对方 rank 写的 token 的最早一步”（第 1 步读位置 prompt_len）
- [x] 核对并记录：现在写回走哪条流、是 memcpy 还是能入图的写 kernel；所依赖的集合通信是全员的，不是 broadcast
- [ ] 哨兵每步校验通过：debug 模式下第 t+1 步开始前本 rank 从 DRAM 读回 token t 整行，与自己 HBM 里的 token t 一致
- [ ] 负测试通过：在对方写回流里注入延迟，关掉等待失败、打开等待通过
- [ ] Graph replay 模式下写回和 event 在捕获范围内，校验通过
- [ ] 写覆盖：两个写者范围并集等于整行且不相交；注册快照 R_am(D) = 8；D 每 NUMA 常驻内存等于 1 份副本；free_ack 在两个成员都停之后
- [ ] 基线表并列，重点看 T_step p50 / p99、T_finish、T_free

## Comments

### 2026-10-30：前置依赖已满足

用户确认02的NPU004验收，01/02均已closed。本票可开始实现，当前尚未实施或进行本票NPU验收，State保持open。

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。

### 2026-10-31：实现与本机交付完成，等待用户NPU验收

基点`cc14b0518105921dd6320358f84cf912f28a9aeb`。
D共享owner分配、同组分工写入、当前token本地供给、历史DRAM读取、逐步哨兵、
Graph延迟写者负测试及Main/SR共享描述符与双读者释放接线已实现。
支持features/layers两种分工及P私有或共享；不新增通用主线功能改动。

写回是当前计算流的`unidex_copy`，位于同层post-attention全员TP通信之前。
复用既有流依赖；不新增跨进程消息、共享标志或通用shutdown改造。
生产同流不需要新的写回event；验收探针单独验证异步写流的event等待。

510项CPU覆盖收齐，完整套件运行一次；23项环境受阻用例定向补跑通过。
19个Python文件静态检查、14模块strict mypy通过；Standards/Spec复审均0项。
评审发现的P无prefix探针回归、D驻留标记、快照完成事件与负测试判据均已修正。
用户澄清“两次”指每轮最多两次，失败修正后可重跑；组合测试修复夹具后已通过。

[源码清单与顺序](../delivery-ticket06.md)、[CPU记录](../verification/ticket06-cpu-20261031/README.md)、
[双机入口](../../../ascend-mempool-test/K1_TICKET06.md)已交付。
NPU尚未运行，State保持open，09的06依赖尚未满足。
入口沿用两轮精简功能范围；V3独立答案、V8注册CPU重放，物理NUMA驻留与性能另记。
旧票性能基线条款按spec移至baselinetest，本轮不增加性能埋点。
