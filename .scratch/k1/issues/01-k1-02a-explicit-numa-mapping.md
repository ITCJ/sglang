# 01: K1-02a 显式映射启动与页驻留证据

**2026-10-29执行范围更新（用户指令优先）：** 性能埋点、perf配置及运行工具仅保留在
`cryang/dev/mempool-min-baselinetest`，当前工程分支只保存历史测试结果。
K1功能实现与NUMA验收继续推进；下文性能复测、基线并列和遗留指标重报条款
须在测试分支另行安排，不计作本分支已实现或已验证。

**What to build:** 启动时给一份 {host, rank, node} 映射文件，服务按它放置每 rank 的池，注册快照与映射文件逐 rank 一致（规划器报告从 planned 变 applied），每份副本的页驻留抽样落在配置的 NUMA；换一个合法映射也照样通过，不合法映射被拒绝并给出拒绝码。副本数仍是 16 / 16 / 16 / 16，池的创建路径不变。细节见 k1-tickets.md 第 5 节。

**Blocked by:** None (can start immediately)

**Status:** needs-info

**State:** open

- [ ] 默认映射（现状的连续映射）下 A / B / C / E 与 V1–V10 全过，perf 子模式基线表每段在轮间波动内
- [ ] 另一个合法映射（把两个 rank 的副本换到别的节点）下同样全过，驻留抽样跟着映射走
- [ ] 不合法映射（节点不存在、rank 重复、缺项）启动被拒绝，拒绝码正确
- [ ] 注册上报每份副本的 NUMA 与 owner，规划器报告状态为 applied，定义是逐 rank 注册快照等于映射文件
- [ ] 页驻留抽样：每份副本的抽样页在配置的 NUMA；每 NUMA 常驻内存等于两份私有副本
- [ ] 拓扑快照包含映射文件、注册快照、驻留抽样结果；基线表与票 01 并列，重报三项基线遗留

## Comments

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。

### 2026-10-29：实现交付，等待用户执行 NPU 验收

用户要求实现本票，并确认 TDD 边界为规划器/启动配置、注册与驻留证据、自运行验收 CLI。
已验收的 perf 基线由 `134b22d90d` 单独导入，提交 `7a8a0b955b`。
本票实现不依赖未验收的其他 K1 票，不启用 share=1。

已实现：生产显式映射启动、明确拒绝码、原有私有 BM 池按节点分配、
owner/host/区域驻留证据注册、完整逐 rank 注册核对后报告 applied。
规划器保留 prefix 和 Index K 的容量，三类附属区域继续属于各 rank 私有池。
页驻留采样失败或驱动不暴露时保留 unverified，不声称物理放置通过。

自运行 `numa-acceptance` 在两种映射下各执行完整 V1–V10 和5轮perf，
保存基线并列和三项遗留。非法映射调用生产启动配置入口，模型/BM创建前拒绝。
[代码阅读顺序与目录例外](../delivery-ticket01.md)、[NPU命令与判据](../../../ascend-mempool-test/K1_TICKET01.md)。

Mac 实际验证：独立完整 CPU suite 505 passed、735 subtests passed（57.76秒）。
原有 TestChannel collection warning 保留。8模块strict mypy通过。
CPU检查原始输出见 `../verification/ticket01-cpu-20261029/`。

NPU 未执行，全部硬件验收框保持未勾选，State保持open；等待用户执行NPU验收。
尚待验证两种映射下的模型/Graph正确性、实际页驻留与每NUMA内存、基线差异。
SDK独立GPU注册计时未暴露，报告保留null，不能把其他计时当成该项实测。

交付收尾：最终25项perf/NUMA定向回归、自运行CPU gate 79项通过。
15文件lint/format/isort/3.9语法与bash语法通过，8模块strict mypy通过。
Standards 0项；Spec 1项已修复（旧perf manifest说明），0项未解决。
沙箱首次阻止本地ps/回环socket检查，授权重跑通过；没有NPU执行记录。

### 2026-10-29：按用户要求移除导入的性能代码

用户说明baselinetest分支专门保存性能测试实现，工程分支不得出现纯测性能的添加物。
因此移除导入提交及其工程代码、perf运行/报告/测试工具，仅保留历史结果记录。
公共output_streamer和非K1变更的Ascend模块恢复到`8869c2c605`。
K1 NUMA映射、applied核对、驻留证据与双映射V1–V10功能验收保留。
原`3156916a11`及导入提交已从当前分支历史移除，K1提交已重新生成。
此前505项CPU等记录属于旧版本；当前版本重新检查并单独归档。
未执行NPU，State保持open；性能复测不再由工程分支的入口执行。

移除后的Mac检查：完整476项CPU、735子测试（47.21秒），定向24项、103子测试通过。
14文件静态检查、7模块strict mypy及双角色CLI预览通过；两轴审查均0项。
当前记录见 `../verification/ticket01-no-perf-20261029/`。NPU未执行。

历史核验通过：结果归档提交为`9bb7e186b2`，仅含记录文件；baselinetest仍为`134b22d90d`。
原有未提交文档改动完整保留；本次未推送。
