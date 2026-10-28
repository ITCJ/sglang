# 票 01：阶段 0 单请求对照基线（Ascend NPU）

**Status:** ready-for-user-run
**State:** open
**Blocked by:** 无；用户确认阶段 0 完成，明确授权先做基线。
**Spec:** [spec.md](../spec.md)

## 完成标准

- [ ] 两机各一条 perf 命令，自运行与自动清理，失败非零。
- [ ] 所有指定指标、原始每步、各 rank 结构化日志、版本/配置/拓扑快照。
- [ ] 离线报告可重跑，A/B e2e 拆分、固定等待、稳定性与混淆项完整。
- [ ] 每指标每适用用例至少 4 个有效非预热样本，轻量断言严格通过。
- [ ] 用户运行并确认 NPU 基线；随后才能关闭本票并推进 K1。

## Comments

### 2026-10-28 第 1 步：计时观察点

实现 opt-in perf 配置及 Main、SR、P/D runtime、Ascend 传输事件。
时钟为进程本地 monotonic_ns，设备查询复用现有 tick，不新增同步。
唯一公共功能文件 `scheduler_components/output_streamer.py` 增加默认关闭的发送观察回调：
Ascend hook 能看到 prebuilt token 可用，但看不到非流式实际 IPC 发送；该组件持有唯一发送边界，
所以由 Ascend 服务安装回调来记录 T_first，保留原输出频率。
下一步实现入口与离线统计，随后 CPU/静态检查；NPU 尚未运行。

### 2026-10-28 第2–4步：入口、报告与本地验证完成

新增perf子模式与离线生成器，默认5轮、首轮预热、每轮重启。
采样严格使用128实际输出/length/完整参考IDs/V1/V6；错误样本和可提取计时保留。
真实本地Peer和进程模拟验证B失败→C/E作废→全部子进程退出→下一轮重启。
早期启动/参考失败也生成明确失败报告，不伪造空基线。

完整CPU unittest为472/472，新增perf pytest为12/12；15文件静态与4组CLI通过。
Standards/Spec最终均0项阻断，详见[本地记录](../verification/cpu-20261028/review.md)。
实现、命令与源码阅读顺序见[交付](../delivery.md)。

与普通模型代码不同，双机启动/HTTP请求/离线统计必须在服务外工具执行，
因此工具沿用既有ascend-mempool-test目录；公共output_streamer hook理由见第1步记录。

**等待用户执行NPU验收。** 尚无真实性能样本、拓扑实测或baseline.md硬件报告。
所有硬件完成项保持未勾选，ticket保持open；不解锁K1功能实现。

### baseline01-001 回传与用户更新输出规则

首次真实NPU运行前4轮B核心答案均正确，目标完整IDs跨轮一致，但从索引30起与参考不同；
旧规则使C/E跳过，第5轮又因BadStatusLine中断。原失败记录保留，详见
[回传分析](../verification/baseline01-001/diagnosis.md)。readiness修复已提交ddbee124b2。

用户明确允许完整IDs差异只记录，并要求继续5轮ABCE、首轮预热、轮间整套重启。
新core-answer-v1分别核对参考/实际开头JSON的独立标准答案，保留全部续写；
逻辑或结构失败记录但不主动跳过后续请求。离线报告保存首差和跨轮稳定性，
output-analysis.json包含完整参考/实际输出并传给D端。最终验收由用户决定。
新规则尚待NPU重跑；不因本地检查通过关闭本票。
