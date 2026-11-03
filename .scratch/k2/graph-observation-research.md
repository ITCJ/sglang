# ticket13：NPU Graph 重放观测接口核查

日期：2026-11-03。范围：官方 `Ascend/pytorch`、MindStudio/MSTX 文档与
`Ascend/msprof` 源码记录；没有运行 NPU，也没有证明部署版本已经具备这些能力。

## 结论

建议使用 **`torch_npu.profiler` + 绑定真实 NPU Stream 的 MSTX range**。
profiler 必须在 capture 前已进入采集状态，并保持到被测 replay 结束。
capture 内写静态 `graph_key/layer/phase`；每次 replay 外围另写带
`forward_id` 的设备 range。离线使用设备 MSTX lane 和设备任务时间线。
Python 提交区间只用于控制路径。此方案必须先通过同图两次 replay 的实机关口，
不能把“API 调用成功”当作重放归属已成立。

## 1. 最小可用 Python 接口

当前官方源码提供 `mstx.mark(message, stream=None)`、
`mstx.range_start(message, stream=None)` 和 `mstx.range_end(range_id)`。
`stream=None` 走 host 路径；传入真正的 `torch.npu.Stream` 才传递设备 stream。
Python 包装会吞掉异常并打印提示，因此 gate 必须检查导出记录本身。
见 [MSTX Python 实现](https://github.com/Ascend/pytorch/blob/master/torch_npu/npu/mstx.py)。

```python
experimental_config = torch_npu.profiler._ExperimentalConfig(
    profiler_level=torch_npu.profiler.ProfilerLevel.Level1,
    mstx=True,
    export_type=[torch_npu.profiler.ExportType.Text],
)
prof = torch_npu.profiler.profile(
    activities=[
        torch_npu.profiler.ProfilerActivity.CPU,
        torch_npu.profiler.ProfilerActivity.NPU,
    ],
    experimental_config=experimental_config,
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(output_dir),
)
prof.start()  # 必须早于包含语义 range 的 graph capture

# capture 中；需在该 phase 实际使用的 stream context 中执行。
range_id = torch.npu.mstx.range_start(
    "k2:g=bs4:l=3:phase=attention", torch.npu.current_stream()
)
attention(...)
torch.npu.mstx.range_end(range_id)

# replay 外围的同类 range 使用本次 forward_id，不写进静态 graph 标签。
# 完成所有被测 replay 后，才结束 profiler 并离线分析导出数据。
prof.stop()
```

这段只表达 API 与生命周期，并非已经过部署版本验证的 runnable example。
`_ExperimentalConfig` 确有 `mstx`、Level1、Text 导出参数；
`mstx` 最终映射到 C++ 的 `msprof_tx`。
见 [配置源码](https://github.com/Ascend/pytorch/blob/master/torch_npu/profiler/experimental_config.py)。

**关键接线约束：** C++ `MstxMgr` 在未启用 profiler/MSTX 时直接返回；
设备 stream 的 mark/range 则通过 `OpCommand::RunOpApiV2` 排入队列。
因此先 capture、后启动 profiler 不会补回 capture 时被跳过的语义任务。
见 [MSTX C++ 实现](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/profiler/mstx_mgr.cpp)。

官方 `mstxMarkA` 文档同样区分空 stream 的 host 记录与有效 stream 的设备记录。
绑定当前主流不能代替 miss stream 内的打点。
见 [mstxMarkA API](https://www.hiascend.com/document/detail/zh/mindstudio/2610/msTX/mstxAPIReference/docs/zh/api_reference/Common/mstxMarkA.md)。

## 2. Capture 标记确实能够进入 replay，但存在已知解析版本问题

`Ascend/msprof` 的官方变更说明明确描述：capture 中的 MSTX 任务被图捕获，
多次 replay 产生重复 `index_id`。旧解析按同 id 最早/最晚时间合并，
会制造横跨多次 replay 的错误 range；旧 `tx.csv` 还只保留重复 mark 的末条。
这与“每个 layer 的 range 只出现一次”不能混同。
见 [官方 msprof 源码目录内的 `fix mstx data analysis err in acl graph scene` 变更记录](https://gitcode.com/Ascend/msprof/tree/master/analysis/msmodel)。

该变更说明针对 CANN 9.1.0 以后的新 mark/range tag 分流处理，并按时序配对重复
range、保留重复 mark；更早数据保留原处理路径。这只是上游修复的版本线索，
不能据此宣布安装了某个 CANN 版本就已通过。两次 replay gate 仍要核对原始导出。
同一来源未能通过当前浏览工具取得固定 commit permalink，故这里保留证据边界。

不建议为绕过旧 range 解析而静默换成成对 mark：旧导出同样可能丢重复 mark。
若部署工具链不能提供完整重放记录，本项保持 incomplete。

## 3. Graph task 映射的证据边界

官方 profiler 解析器读取 `msprof_*.json`，使用 `HostToDevice` 的 flow 起止
关联 ACL 调用与设备事件；设备事件以 `pid/tid/ts` 定位。
它没有证明“capture 中的 Python layer scope 会在 replay 自动重新产生一次”。
见 [CANN 时间线解析器](https://github.com/Ascend/pytorch/blob/master/torch_npu/profiler/analysis/prof_parse/_cann_file_parser.py)。

此次没有找到一个可依赖的公开 Python API，能返回稳定的
`capture task id -> 每次 replay task instance -> layer` 映射。
因此建议以捕获的 stream-bound MSTX 语义 range 为主、设备任务和 flow 为证据，
不要依赖重复 kernel 名或假设 task id 唯一。

设备 MSTX lane 的命名为 `Stream <id> MSTX domain <domainid>`，与 host MSTX lane
不同。离线必须先识别设备 lane，再取范围；不得把二者混在一起。
见 [Ascend msinsight 系统调优文档](https://github.com/Ascend/msinsight/blob/master/docs/zh/user_guide/system_tuning.md)。

## 4. 普通 Event 不是零同步的替代接口

当前 Python `Event` 创建 `graph_external=False`；`ExternalEvent` 创建
`graph_external=True`，但固定 `enable_timing=False`。不能照搬 CUDA
`Event(external=True, enable_timing=True)`。
见 [Event/ExternalEvent 定义](https://github.com/Ascend/pytorch/blob/master/torch_npu/npu/streams.py)。

当前 C++ `elapsed_time` 先执行 `emptyAllNPUStream()`，再同步两个 event；
`recorded_time` 同样先清队列并同步 event。即使调用者认为某一步已经完成，
这些调用仍会发生，也不能由此取得此前每次 replay 的独立历史记录。
因此本票不在逐层路径读取它们，也不把完成后一次读取视作重放快照保证。
见 [NPUEvent 实现](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/core/npu/NPUEvent.cpp)。

此次未找到官方保证：普通 timing event 在同图连续 replay 下可无额外同步读取、
并保留每次 replay 的独立时间戳。该项属于未证明能力，不作为备选计时方案。

## 5. 结合本仓库的实施建议

本地 `NPUGraphRunner._capture_graph` 在 `torch.npu.graph(...,
auto_dispatch_capture=True)` 内调用模型；Python layer wrapper 在 capture 执行，
replay 不会重新执行这些 wrapper。当前 runner 的 profiler context 是已有的
capture/profile-memory 用途，并非服务全程 replay profiler。
见 [本地 NPUGraphRunner](../../python/sglang/srt/hardware_backend/npu/graph_runner/npu_graph_runner.py)。

最小实机 gate 应同时证明：

1. 同一张图两次 replay 产生两组完整、可区分的设备 layer/phase range，未被合并。
2. 动态 `forward_id` 外围 range 能唯一关联对应 replay；连续提交仍可区分。
3. miss stream 的 device range 与主流分开，wait 设备任务存在；host 提交用时不冒充等待。
4. 相同 kernel 名的不同 layer 仍靠语义 range 区分，设备任务均落入预期范围。
5. 两份页计数完成快照保留各自输入的结果，后一个 replay 不覆盖前一个。

后续实际模型的 HCCL、indexer、attention、MoE 归属仍需一次真实时间线核对。
MSTX 的区间自身不能直接给出跨流依赖关键路径；没有任务/依赖证据的类别保持缺项，
不能用总范围或 host 时长自动补齐。

交付状态：资料核查完成；接口接线建议已提供；NPU Graph replay gate 未运行，
逐层设备计时及 forward_id 归属尚未验收。
