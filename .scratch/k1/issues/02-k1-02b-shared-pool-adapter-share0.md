# 02: K1-02b 共享池适配器接入（share=0）与记账骨架

**What to build:** 服务进程通过共享池适配器创建和使用池，成员只有自己，行为与阶段 0 完全相同；多成员会话接口替换两参与者会话，端口、共享库生命周期、计算图下的池访问、请求释放和关闭路径都接到适配器上。同时落地三个行为不变的预重构：协调者按副本维护覆盖位图并由它算 ready_tokens（单写者下与原连续计数器结果一致）；协调者按副本维护读者计数，归零才发 free_ack（现状计数 1）；注册上报四个副本数和每份副本的 owner 与成员列表。细节见 k1-tickets.md 第 6 节。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

**State:** closed

- [x] 两轮 session-acceptance 的 A/B/C/E、内容、Graph、复用、长生成和提前EOS通过；V3独立答案与V8注册CPU重放按用户授权精简
- [x] ready_tokens 与 free_ack 的时间线与阶段 0 一致；同一轮里位图算法与原计数器算法的 ready_tokens 序列逐事件相同
- [x] 沿用SGLang默认父进程清理；双端进程清理、端口释放及同端口重启通过；按用户决定不要求worker主动退出或逐rank BM关闭回执
- [x] 注册快照含四个副本数和每份副本的 owner、成员列表
- [x] 用户确认精简范围和NPU结果后关闭；启动耗时与性能对照按既定约定另留测试分支

## Comments

### 2026-10-30：用户确认NPU004验收通过，关闭票据

用户运行`k1-ticket02-004`后明确回复“没问题，可以验收”。本轮交付版本`fa97a49820`，31/32双机，沿用SGLang默认退出和两轮精简验收。
按用户确认勾选本票验收项，State=closed；05/06/07的01、02直接依赖均已满足。
[用户确认、命令及证据边界](../verification/k1-ticket02-004-20261030/acceptance.md)已记录。
完整004 JSON未回传，本记录以用户验收确认为依据，不冒充agent独立检查；002/003失败历史保留。


### 2026-10-30：用户决定沿用默认退出，撤回非必要主线改动

用户明确要求shutdown依靠原有父进程清理worker，不修改非必要主线代码，已写入AGENTS.md。
恢复本票前的scheduler与P/D四个循环；移除service主动BM关闭、quiesce协议、退出阶段日志及相关测试。
独立池适配器、NUMA、ready/free记账与两轮精简验收保留，验收按实际进程清理、端口释放及重启判定。
不改写NPU002/003历史失败报告，撤回的主动关闭要求不再作为本票门槛。
尚无用户对恢复后版本的NPU验收确认，State保持open。

### 2026-10-29：NPU003 最终退出阶段分析

用户回传MR/SR已quiesced，两侧全rank的BM、host与distributed阶段标记齐全；
D仍有16个、P仍有8个scheduler在父进程15秒期限后被强制清理。
核对发现distributed标记只证明abort helper返回，其缺少API/吞异常分支也会返回，不能证明HCCL销毁。
剩余范围为函数返回时对象析构与multiprocessing/线程/native收尾，需要退出窗口调用栈区分。
本轮按用户要求仅分析，未修改实现；003仍未通过，State保持open。

### 2026-10-29：NPU003 正常关闭日志核验失败

用户回传 `prefill: normal shutdown used forced cleanup or crashed`。
失败发生于汇总核验，需读取具体错误标记和新退出阶段回执；尚不能确定BM后清理停在哪一步。
D日志已由本次入口收集到31，下一步只读现有日志，不重跑验收。
[故障记录](../verification/k1-ticket02-003-20261029/diagnosis.md)。NPU003未通过，State保持open。

### 2026-10-29：受控停机修复交付，等待 NPU003

MR先停止接单，等当前请求双方free_ack后请求SR停机；两侧全部rank确认空闲，才停止控制tick并进入D→P关闭。
真实TCP/ZMQ复现先失败后通过，未经确认的断连仍fail-stop。
验收保留两轮，新增control-quiesced与全部rank后续通信清理回执；强杀或崩溃不能算正常关闭。
完整490项CPU（48.227秒）、6模块strict mypy和两轴复审通过。
[验证记录](../verification/ticket02-quiesce-cpu-20261029/README.md)。
NPU002的D后续滞留、native double free尚未确认解决；新阶段回执帮助定位，待用户运行003，State保持open。

### 2026-10-29：NPU002 关闭传播链已复现，仅分析

补充日志显示 D 的 16 个 rank 已完成 BM 关闭，随后 scheduler 被强制回收，P 收到
`Host unreachable` 并 fail-stop。本机真实 ZMQ/Main/SR 在约2秒内复现 D 断开→Main故障→P全部SR故障。
正常关闭缺少 Main/SR 受控停机协调；P 的60秒崩溃诊断等待解释外层超时。
D 的 BM 关闭后滞留与 P 的 native double free 仍需进一步定位。
用户本轮要求先分析，未修改功能代码；完整依据见下方故障记录，State仍open。

### 2026-10-29：NPU002 第二轮 P 正常关闭超时

用户回传两轮服务已启动，最终报 `prefill graceful shutdown exceeded 60s`。
已核对验收入口等待的是 P 主进程退出；尚不能确定卡在请求排空、BM 关闭或进程回收。
[故障记录与诊断边界](../verification/k1-ticket02-002-20261029/diagnosis.md)已归档。
等待两侧 restart 服务日志建立复现；本轮未通过，State 保持 open。

### 2026-10-29：实现交付，等待用户 NPU 验收

已有NUMA分配、规划器和共享组件已复用；服务按PoolSession建池。
删除旧rank-pair包装及实验中重复的join/owner映射代码。
按副本计算页覆盖与读者计数，注册补齐owner/members/session；share=0与副本数保持不变。
正常关闭接入设备同步和BM释放，故障路径保留进程退出回收。

自运行session-acceptance固定两轮，复用上一票配置即可运行。
按用户要求精简V3/V8并明确报告边界；不增加性能代码。
完整CPU回归及审查修复证据见[验证记录](../verification/ticket02-cpu-20261029/README.md)。
[源码阅读顺序及目录例外](../delivery-ticket02.md)、[NPU运行命令](../../../ascend-mempool-test/K1_TICKET02.md)。
尚未执行NPU测试，以上硬件验收项均未勾选；State保持open，05/06/07仍待本票验收。

### 2026-10-29：用户授权实现与精简验收

用户授权复用已有 NUMA 分配、分组规划及共享池组件，删除接入后多余代码。
重复运行最多两轮，精简繁杂测试并减少模型重启；性能工具仍留在测试分支。
本票实现、测试精简与交付记录合为一个提交，基点为 `993ae38ed6`。

用户确认三个 TDD 接口：池会话启动/映射/关闭；SR 对外 ready_tokens、
free_ack 与注册结果；自运行验收入口的轮次与结果汇总。
保持 share=0，完成后仍等待用户 NPU 验收，State 保持 open。

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
