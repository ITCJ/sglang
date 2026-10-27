# 基线票01交付与源码阅读顺序

实现分支：`cryang/dev/mempool-min-baselinetest`；基点：`8869c2c605`。
工作区：`/private/tmp/sglang-mempool-perf-20261028`。

运行命令、失败判据、归档路径和观察限制统一见
[Ascend NPU 基线运行说明](../../ascend-mempool-test/MEMPOOL_BASELINE.md)。
本票真实基线尚待用户运行，State保持open；K1未实施。

## 功能源码阅读顺序

1. [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py)：perf配置默认关闭，拒绝重型验证混入计时。
2. [mempool_perf.py](../../python/sglang/srt/disaggregation/ascend/mempool_perf.py)：单进程事件身份、单调时钟、非阻塞设备完成与首个真实输出。
3. [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py)：命令排队、publish与零页跳过观察点。
4. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：Main实际收发/HTTP响应边界，给Ascend各对象安装同一进程observer。
5. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：dispatch收到、恢复完成、prompt/gate就绪、finished发出。
6. [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：forward开始、完成事件、主KV与Index K写回完成；每步不强制同步。
7. [conn.py](../../python/sglang/srt/disaggregation/ascend/conn.py)：Index K发送入口与原生receiver成功观察。
8. [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py)：handoff完成、三条件观察与实际HBM释放。
9. [output_streamer.py](../../python/sglang/srt/managers/scheduler_components/output_streamer.py)：默认关闭的实际输出发送回调。

## Ascend目录之外的功能变化

公共输出组件持有唯一IPC发送点。Ascend hook能看到prebuilt token可用，
但没有原生hook可观察非流式何时真正发送。新增可选callback由Ascend perf服务安装，
默认None；保持原有发送频率与内容。

独立Ascend测试工具位于既有`ascend-mempool-test/scripts/`：
`run_mempool_min.py`增加子模式；`mempool_perf_runner.py`管理双机重复/归档；
`mempool_perf_report.py`离线校验统计。启动、HTTP请求和离线重算必须由服务外工具完成，
不能放在NPU模型hook里；这些工具不作为模型运行时代码导入。

本地检查：472项unittest、12项perf pytest、15文件静态与4组CLI描述通过；两轴复核无阻断。
硬件尚未执行。

## 核对重点

正常路径：空prefix → A保存32页 → B恢复4096并补8页 → C恢复3968且零页保存 → E冷算且满池跳过保存。
每请求等真实finished、publish与双端free_ack后才返回Main响应并发下一条。
本票只旁观现有所有权与释放流程，不改变slot分配、prefix保护或数据拷贝策略。

正确性与计时证据分别归档；无效样本保留可提取的计时但不入统计。
CPU验证、两轴审查与尚待执行的NPU验收记录在[票01](issues/01-stage0-single-request.md)。
