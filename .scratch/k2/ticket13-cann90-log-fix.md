# Ticket13：CANN 9.0.0 日志兼容修复

基点：`ef4bc86dfa`。用户要求继续使用 CANN 9.0.0，只做日志补丁。
已上传的 Graph 实测数据离线检查通过；完整模型 NPU 验收仍待执行。

## 已确认根因

包内版本：torch_npu 2.10.0、CANN 9.0.0。
原始 `ts_track.data.0.slice_0` 有28条设备打点。
index_id 5、6、7、8分别有四个打点，对应每个图内阶段两轮的起止边界。
旧导出只保留最早开始到最晚结束，因而横跨两次 forward。

官方源码固定版本：
[msprof 222074c](https://github.com/Ascend/msprof/blob/222074cdb867c23963bb1e29af6a387cd3d2b90a/analysis/msmodel/msproftx/msproftx_model.py)。
其中 pre-9.1 分支按 index_id 合并首尾，9.1 分支才区分 mark/range 并逐对恢复。
仅更新该上游解析器仍会对9.0走旧分支，因此本补丁明确处理本项目的9.0 start/end范围。
记录布局核对同一版本的 `data_struct_size_constant.py`、`step_trace.py`、
`msproftx_decoder.py`、`capture_stream_info_bean.py`。

## 恢复口径

只处理版本为9.0.x、且保留原始数据的本项目 `mempool:` start/end 范围。
通过主机记录的 mark_id 对应设备 index_id；按实际设备时间排列后逐对配对。
使用设备记录的 `hwts_frequency=49.999001 MHz` 换算 tick 差。
已导出的设备起点保留校准后的绝对时间；检查恢复首尾与旧导出相差不超过2ns。
缺端点、身份歧义或时钟不符均失败，绝不按总时长均分。

保留真实物理 stream；额外保存捕获记录给出的 logical_stream。
本例44→12是主流，43→11是miss侧流。设备时间分解使用映射后的流归属。
恢复结果带 `timing_source`、`raw_index_id`、`raw_source`，不覆盖原始 trace。
缺少原始数据时仍保留原导出，现有完整性检查继续拒绝缺失的逐步范围。

| 阶段 | replay 1，μs | replay 2，μs |
|---|---:|---:|
| cache_manage | 396.948 | 362.827 |
| miss_io | 413.088 | 370.867 |
| materialize_wait | 467.629 | 420.188 |
| attention | 395.648 | 358.787 |

这些是日志原始端点恢复的设备范围，不是正式模型性能数据；不同流范围不可直接相加。

## 本轮净改动与验证

Ascend/NPU功能源码：无。通用主线功能源码：无。

脚本按阅读顺序：

1. `ascend-mempool-test/scripts/observation_legacy_mstx.py`：新增CANN9.0原始记录恢复与捕获流映射。
2. `ascend-mempool-test/scripts/analyze_k2_observation.py`：设备范围解析后调用恢复；时间分解按映射后的流归属。

测试：`test_observation_legacy_trace.py`和`tests/fixtures/mstx-cann90/`。
说明：本页、issue、memory、target-alignment D与检查记录。

新增三项回归与原有十五项观测测试共18项通过。
Spec评审发现HCCL物理流也需映射；已修复并补充1μs通信归属测试。
测试初次fixture生成将顶层list误作dict，修正后直接复现原始两轮缺项，再完成修复。
每个unittest最多两轮；未重跑完整套件。
ruff、isort、Python3.9 AST与两个脚本严格mypy通过（外部导入忽略）。
原始379KB压缩包实际离线重分析passed；报告见
`verification/ticket13-cann90-log-fix-20261103/recovered-graph-report.json`。
代理没有运行NPU，没有改变CANN、profiler启停、Graph执行、同步或服务退出方式。

## 31 复核命令

```bash
cd /home/cryang/sglang
git pull --ff-only origin cryang/dev/mempool-min
python3 -u ascend-mempool-test/scripts/verify_observation_graph.py \
  --log-dir /home/cryang/log/k2/k2-ticket13-001/graph-prefill \
  --analyze-only
```

预期退出0，status=passed，两轮 missing=[]。
报告为 `graph-prefill/graph-trace-analysis.json`，无需重采集。
正常Graph gate和完整模型离线分析共用同一修复，无需额外开关。
完整双机验收需换新run-id，仍等待用户执行后确认；ticket13保持open。
