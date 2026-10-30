# 05: K1-03a P alive 主 KV 共享

**What to build:** P 侧打开 share=1：组 {2k, 2k+1} 共用一份 alive 主 KV，owner 是 rank 2k，两个成员按页分写（默认连续两半，可配）；D 的两个 rank 经各自与 P_i 的连接读同一份，读到的页有一半是对方 P rank 写的；就绪由两个分片的覆盖位图合并得出，释放要等两个读者都停；op1 由组内两分片按页分担填进共享副本，op3 的源改为共享副本；P 侧从 16 份变 8 份，D 侧仍 16 份私有副本。细节见 k1-tickets.md 第 9 节。

**Blocked by:** [01（K1-02a）](01-k1-02a-explicit-numa-mapping.md)、[02（K1-02b）](02-k1-02b-shared-pool-adapter-share0.md)（均已用户验收关闭，依赖已满足）

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V4 加“D 读到对方 P rank 写的页”的核对；V9 改为两个写者都 free 后才复用存储槽
- [ ] 注册快照 R_am(P) = 8，每份有 owner、NUMA、两个成员
- [ ] 哨兵跨进程写读通过：rank 2k+1 在自己那半页写入模式，rank 2k 经自己的虚拟地址读回一致
- [ ] P 每 NUMA 常驻内存等于 1 份副本，总量等于 8 份
- [ ] 写覆盖：两个写者范围并集等于整页且不相交，静态检查和运行时字节计数都通过
- [ ] ready_tokens 由两个分片的位图合并得出，任一分片单独报完不会让它越过未写的页；free_ack 在两个 D 读者都停之后
- [ ] 基线表并列，重点看 T_wt_rank、T_ready、T_tail、T_free

## Comments

### 2026-10-30：实现交付，等待用户 NPU 验收

P主KV按组共享，P Index K、prefix与D主KV仍私有；按页分写、分工fill、共享源admit已接通。
SR按副本合并完成位图与双读者，Main去重共享描述符，默认退出路径未改。
验收入口`p-sharing-acceptance`复用两轮服务，增加共享哨兵、实际写计数与分配容量核对。

502项CPU覆盖最终全部通过；25文件静态检查、15模块strict mypy通过。
首次回归21项权限错误及1项fixture遗漏已单独补跑通过；评审后新增路由回归红绿通过。
Standards初审1项命名建议、Spec初审1项入口问题均修正；复审两轴均0项。

[源码阅读顺序](../delivery-ticket05.md)、[本机验证与次数](../verification/ticket05-cpu-20261030/README.md)、
[逐机命令与覆盖边界](../../../ascend-mempool-test/K1_TICKET05.md)。
本次无通用主线功能改动；测试分支未改。NPU尚未运行，State保持open，验收项暂不勾选。
两轮入口为精简功能验收；物理NUMA驻留若unverified则保留，性能按既定分支约定另行安排。

### 2026-10-30：用户授权开始实施

基点`4c0e79b55046ba69a1559eaab39d061ce3eec8ed`，当前分支逐票提交。
沿用已确认的池会话、SR公开结果、自运行验收入口三个TDD接口。
首先实现P主KV共享且其余区域私有的会话布局；新CPU行为先失败后通过。
硬件验收尚未运行；性能工具继续留在测试分支，State保持open。

### 2026-10-30：前置依赖已满足

用户确认02的NPU004验收，01/02均已closed。本票可开始实现，当前尚未实施或进行本票NPU验收，State保持open。

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
