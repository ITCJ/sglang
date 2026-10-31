# 06: K1-03b D alive 主 KV 共享与每步顺序

**What to build:** D 侧打开 share=1：组 {2k, 2k+1} 共用一份 alive 主 KV，每层算完两个成员各写新 token 这一层的一半特征维（可配为按层切）；下一步任一成员 swap_in 读到对方写的那一半是正确的。顺序靠写回排在本 rank 本层第一次全员集合通信之前来保证（同流天然成立，单独流则 compute stream 等写回 event），不加跨进程消息；T_step 超出轮间波动再换“本步最后一次全员集合通信前等齐”的放宽版本。D 侧从 16 份变 8 份；不依赖 P 侧共享。细节见 k1-tickets.md 第 10 节。

**Blocked by:** [01（K1-02a）](01-k1-02a-explicit-numa-mapping.md)、[02（K1-02b）](02-k1-02b-shared-pool-adapter-share0.md)（均已用户验收关闭，依赖已满足）

**Status:** ready-for-agent

**State:** closed

- [x] 用户接受的精简功能范围：A/B/C/E与V1–V10；V3独立答案、V8 CPU注册重放；V4覆盖第1步读prompt_len（完整边界见验收确认）
- [x] 核对并记录：现在写回走哪条流、是 memcpy 还是能入图的写 kernel；所依赖的集合通信是全员的，不是 broadcast
- [x] 哨兵每步校验通过：debug 模式下第 t+1 步开始前本 rank 从 DRAM 读回 token t 整行，与自己 HBM 里的 token t 一致
- [x] 负测试通过：在对方写回流里注入延迟，关掉等待失败、打开等待通过
- [x] Graph replay 模式下写回和 event 在捕获范围内，校验通过
- [x] 分写覆盖整行；注册R_am(D)=8；每NUMA分配一份副本；free_ack等全部读者停止
- [ ] 物理NUMA页驻留：未验证，作为用户接受的证据限制保留
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

### 2026-10-31：NPU001自动化精简功能验收通过，待用户确认关闭

用户回传9b760a1add在31/32的prefill summary，内嵌acceptance完整。
两轮各6请求、P/D主KV各8份、D分配8GiB、全部16rank逐步哨兵与写字节通过。
192份回执合计491712次逐层整行校验；Graph检查按验收器通过链确认。
原始Graph探针明细和32独立summary未回传，不补造逐项证据。

释放读者归零、槽复用、超时fail-stop及双端默认进程清理通过。
物理NUMA驻留、性能、V3/V8及P侧DRAM读回限制保留。
本次硬件仅覆盖features写分工；layers及P私有+D共享保留CPU验证边界。
[回传摘要、算术核对与限制](../verification/k1-ticket06-001-20261031/README.md)。

原始user_accepted=false保留，State仍open；尚未解锁09的06依赖。
本轮没有修改功能代码或重跑测试。

### 2026-10-31：用户确认验收并授权清理

用户明确回复“验收，并且开始从工程代码中清理测试代码”。
按NPU001精简范围关闭06，09的06依赖满足；08依赖仍未满足。
[验收确认](../verification/k1-ticket06-001-20261031/acceptance.md)记录版本、命令、环境与边界。
逐步DRAM哨兵、Graph延迟负测试和工程计数将按授权撤下；功能路径保留。
硬件证据属于9b760a1add；清理后验证单独记录，不自动扩大NPU结论。

### 2026-10-31：验收后清理完成

本票临时工程探针、额外逐步DRAM读回、统计快照和verify_decode已删除；
旧d-sharing-acceptance撤下，历史复现版本与独立离线报告核验保留。
共享功能、当前KV本地供给、全rank释放门控及默认退出保持。
完整CPU单轮510项通过（48.412秒），11文件静态、13模块strict mypy及两轴复审通过。
[清理记录](../verification/ticket06-cleanup-20261031/README.md)说明裁减的哨兵子场景。
清理后未重跑NPU，硬件结论仍限定在9b760a1add。
