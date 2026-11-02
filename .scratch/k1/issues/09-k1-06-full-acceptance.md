# 09: K1-06 完整验收

**What to build:** 在最终版本（8 / 8 / 1 / 1 / 1，split_read 按段，interleave 开，票 12 的 page first 布局，票 11 的搬运路径）上重跑阶段 0 全部验收项和补充用例（长生成到 G_max、提前结束、入口拒绝四例、超时、双端清理），把四类新证据合在一起核对，基线表与票 01 并列并逐段解释变差，重报三项基线遗留，归档拓扑与版本快照。用户在真实环境核对后关闭，K1 结束。细节见 k1-tickets.md 第 13 节。

**Blocked by:** [06（K1-03b D alive 主 KV 共享与每步顺序）](06-k1-03b-d-alive-main-kv-sharing-step-order.md)、[08（K1-05b prefix 单份：8 段、numa interleave、按段读者、并行 admit、直接 fill）](08-k1-05b-prefix-single-copy-numa-interleave.md)、[11（K1-02c admit 与 fill 的池间搬运路径与分片执行器（行为不变））](11-k1-02c-pool-copy-executor.md)（06、08、11均已closed，经11传递依赖的12也已closed；前置全部满足）

**Status:** ready-for-agent

**State:** open

- [ ] 四组分别完整验收：`(page_size,S_D)=(128,128)/(128,512)/(64,128)/(64,512)`；执行细化见[实施计划](../ticket09-implementation-plan.md)
- [ ] A / B / C / E 与按 05、06、07、08、12、11 调整后的 V1–V10 全过
- [ ] 补充用例全过：长生成、提前结束、入口拒绝四例、超时、双端清理
- [ ] 四类证据合核通过：注册 = 映射 = 驻留；写覆盖；每步顺序的哨兵与负测试；读字节计数
- [ ] 基线表与票 01 并列，每段变差有解释；三项基线遗留重报
- [ ] 拓扑快照与版本快照归档
- [ ] 布局证据：注册 / describe 为 page first、启动签名含布局；真实多请求并发不在本票（阶段 3a，M54），物理 NUMA 驻留与性能单独标记

## Comments


### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。

### 2026-10-31：06依赖满足

用户确认ticket06 NPU001精简功能验收并关闭06。08仍未验收；本票保持open，尚未实施。

### 2026-11-02：同步page first改序定稿

同步 page first 最终布局、布局签名验收与调整后的 V1–V10 范围。直接依赖仍为06、08、11，经11传递依赖12；真实多请求并发属于阶段3a。本票未实施，State=open。性能条款仍受本地spec开头的测试分支约定约束。

### 2026-11-02：11用户确认关闭，本票解锁

用户接受11的31/32工程002验收并明确关闭11；06、08、11直接依赖与12传递依赖全部满足。
本票Status=ready-for-agent、State=open，已解锁但尚未实施。
继承11的[确认范围与证据边界](../verification/k1-ticket11-002/README.md)，不将未测性能/真实SDK在途故障/生产目标独立全量读回写成已通过。

### 2026-11-02：用户指定四组矩阵，计划已编写

用户要求保持原ticket09验证思路，覆盖B=128/64与S_D=128/512的四个组合。
[计划](../ticket09-implementation-plan.md)保留完整ABCE、V1–V10、补充用例及K1合核。
prefix保持5120-token容量，分别40/80页；长生成上限随S_D。
已核实RouterConfig也限制B=128、S_D=128，实施需先放行；不能只改测试脚本。
原D哨兵已清理，计划按验收开关恢复并适配page first；当前尚未恢复。
保留独立reference、真实容量故障及原证据边界。四组均not-run，本轮仅文档规划。

### 2026-11-02：四组完整验收代码交付

用户授权按计划实施。配置、参数化检查、D哨兵/末行、Graph顺序探针与四组自运行入口已接通。
[运行说明](../../../ascend-mempool-test/K1_TICKET09.md)、[CPU与review](../verification/ticket09-cpu/README.md)。
完整CPU553项，已修正本次报告夹具；关键39项与沙箱补跑21项通过，既有UINT32_MAX旧预期失败保留。
四组NPU均未运行；State=open，等待用户真实验收与确认，不勾选硬件验收项。

### 2026-11-02：NPU001第一组通过，第二组preflight端口失败

用户回传两侧b128-sd128 passed，第二组D的10000 occupied，后两组not-run。
本地修复测试入口遗漏dist-init端口及单次bind误报路径，复用已有TIME_WAIT/监听判别。
两项本机回归通过，硬件原因尚无端口快照；详见[001记录](../verification/k1-ticket09-001/README.md)。
新增--start-from支持002仅跑剩余三组，第一组保留001证据。09仍open。

### 2026-11-02：NPU002第二组通过，64/128证据解析失败

用户回传128/512两侧passed；64/128在target报JSONDecodeError(char4964)，64/512未运行。
[002记录](../verification/k1-ticket09-002/README.md)保留摘要边界；先取已有日志，不安排第三轮。
09保持open，尚未定位具体JSON来源。

### 2026-11-02：确认长JSON交错并修复证据输出

用户提供TP3/TP13 JSON中插入TP5/TP0日志头，已确认多进程长日志交错。
验收输出改为独立prefix-content JSON文件+短日志引用，检查器保留原行号与完整内容校验。
11项CPU、8模块Linux目标strict mypy、ruff通过，Standards/Spec无阻断。
003仅继续64两组；64/128为第二次尝试，64/512首次。09仍open。
