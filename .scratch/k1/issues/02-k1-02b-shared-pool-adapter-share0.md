# 02: K1-02b 共享池适配器接入（share=0）与记账骨架

**What to build:** 服务进程通过共享池适配器创建和使用池，成员只有自己，行为与阶段 0 完全相同；多成员会话接口替换两参与者会话，端口、共享库生命周期、计算图下的池访问、请求释放和关闭路径都接到适配器上。同时落地三个行为不变的预重构：协调者按副本维护覆盖位图并由它算 ready_tokens（单写者下与原连续计数器结果一致）；协调者按副本维护读者计数，归零才发 free_ack（现状计数 1）；注册上报四个副本数和每份副本的 owner 与成员列表。细节见 k1-tickets.md 第 6 节。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

**State:** open

- [ ] 两轮 session-acceptance 的 A/B/C/E、内容、Graph、复用、长生成和提前EOS通过；V3独立答案与V8注册CPU重放按用户授权精简
- [ ] ready_tokens 与 free_ack 的时间线与阶段 0 一致；同一轮里位图算法与原计数器算法的 ready_tokens 序列逐事件相同
- [ ] 正常结束有全部rank的BM关闭回执；fail-stop双端进程清理、端口释放及同端口重启通过；物理DRAM页数证据边界由用户确认
- [ ] 注册快照含四个副本数和每份副本的 owner、成员列表
- [ ] 用户确认精简范围和NPU结果后关闭；启动耗时与性能对照按既定约定另留测试分支

## Comments

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
