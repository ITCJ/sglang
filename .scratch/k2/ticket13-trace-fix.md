# Ticket13：31 首次验收的 trace 解析修复

基点：`13ff2021e8`。用户反馈来自 `k2-ticket13-001/graph-prefill`。
本次修复解析错误；图内 MSTX 区间跨 replay 的问题仍待定位。ticket13 保持 open。

## 用户实测与根因

31 已执行 capture、两次 replay、页计数与快照检查，随后 trace gate 失败。
模型服务没有启动。用户回传了 20 个原始 MSTX 事件，包含 10 个主机和 10 个设备事件。

设备事件的 lane 是 `Stream 44` 等，带 `Physic Stream Id` / `Task Id`。
原解析器强制 lane 名含 MSTX，导致两个设备 forward 都被过滤。
此外，`ts` 为字符串；旧数值运算也会失败，host clock anchor 有同一问题。

图内 cache_manage/miss_io/attention 只有一组区间，跨越两次 forward。
这与重放范围被合并一致，但尚未确定合并发生在哪层。修复后旧 trace 仍须判失败。
不通过更改 profiler 调度、增加同步或放宽完整性门槛掩盖该问题。

## 本次净改动

Ascend/NPU 功能源码：无。通用主线功能源码：无。

脚本与测试按阅读顺序：

| 文件 | 用途 |
|---|---|
| `ascend-mempool-test/scripts/analyze_k2_observation.py` | 兼容带设备任务证据的普通 Stream lane；保留 MSTX lane；排除主机事件。Decimal 统一转换设备时间和主机锚点，避免 epoch 浮点精度损失 |
| `ascend-mempool-test/scripts/verify_observation_graph.py` | 在线/离线共用 trace 检查；逐名核对两个 forward；输出每轮缺失阶段和设备区间；失败保留已通过的页快照证据 |
| `ascend-mempool-test/tests/unit/test_observation_trace.py` | 四项 CPU 回归，包含用户原始六条关键设备事件、旧格式、字符串时间、主机排除、合并负例和正确双 replay 正例 |

说明文档：本页、ticket13 issue、K2 memory、target-alignment D 节及检查记录。

## 验证

本地使用用户回传的六条关键设备事件重现 `accepted_forward_ranges: []`。
两个新回归测试首轮触发时间字段 TypeError；修复后四个新增与原有十一个观测测试全部通过。
原始跨 replay 数据仍返回 failed，并保留两个已识别 forward 的计数及层区间缺项。

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/k1-ticket05-fix-venv/bin/python -m unittest test_observation_trace test_observation -v
```

同一 unittest 最多两轮，未重复完整套件。ruff、isort、Python 3.9 AST 和两脚本 mypy 通过。
mypy 使用 strict 配置及 `--ignore-missing-imports`，本机没有 torch_npu 与独立包 alias 的类型信息。
检查记录：`verification/ticket13-trace-fix-20261103/checks.json`。
代理没有运行 NPU，也未证明部署工具链可以保留两组完整图内区间。

## 31 上直接复核旧日志

同步本次修复后执行；不初始化 NPU、不加载模型，32 无需配合。

```bash
cd /home/cryang/sglang
git pull --ff-only origin cryang/dev/mempool-min
python3 -u ascend-mempool-test/scripts/verify_observation_graph.py \
  --log-dir /home/cryang/log/k2/k2-ticket13-001/graph-prefill \
  --analyze-only
```

该旧 trace 预期仍非零退出，但 `forward_counts` 应为两轮各 1。
具体错误改为图内阶段缺失或跨 replay 合并；查看 `replay_checks` 与 `device_ranges`。
输出单独保存为 `graph-trace-analysis.json`，保留原 `graph-observation.json`。
离线结果只检查 trace，不补发页快照通过证明。

回传新报告和原始 `graph-prefill/profile/` 导出目录，继续定位剩余图内区间问题。
先保留现有日志，不启动完整四组双机验收。
