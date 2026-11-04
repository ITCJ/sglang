# Ticket14：带宽刻画套件交付

**代码已实现，等待用户NPU验收。** 本票保持open，不解锁25。
基点50395d0b3b；本票按单独commit交付，提交号以git log为准。

## 行为与运行

独立程序复用生产BM映射、page-first布局和部署的UniDexCopy。
默认先测remote-write：4K/8K/32K/128K逐层新算主KV，单写/双写、D本地读干扰与无写基线。
full增加读者数、粒度、位置、顺序写干扰、小读/Mooncake和物理驻留抽样。
默认16个P写者分工，共同写一份D第0组alive，P prefix按8段交错；Index K只写P。
连续逐层测试保留源张量至库完成，按实际提交线程wait，记录排队和最后排空。
D通过设备读路径核对写入内容，不直接CPU读取BM主存。
父运行器等两侧测量/报告收集完成后结束自己的worker；没有逐rank释放回执或通用shutdown改动。

**完整31/P、32/D命令、环境、字节口径、失败判据及回传清单：** [BANDWIDTH.md](../../ascend-mempool-test/BANDWIDTH.md)。
默认log目录`/home/cryang/log/k2/<run-id>/prefill|decode/`；重复run-id直接拒绝，不覆盖原报告。

## 功能源码阅读顺序

以下为Ascend/NPU专用的独立微测代码，均在`ascend-mempool-test/src/ascend_mempool/`。

1. [bandwidth.py](../../ascend-mempool-test/src/ascend_mempool/bandwidth.py)：配置、测量档位、字节量与复用PageWriter的分工。
2. [bandwidth_device.py](../../ascend-mempool-test/src/ascend_mempool/bandwidth_device.py)：复用生产会话/布局，真实L2G与UniDexCopy执行、内容核对、逐层队列。
3. [bandwidth_report.py](../../ascend-mempool-test/src/ascend_mempool/bandwidth_report.py)：并发时间跨度、三次中位数、合并样本p99、全写者首操作完成统计与覆盖/字节校验。
4. [sharing_control.py](../../ascend-mempool-test/src/ascend_mempool/sharing_control.py)：既有独立测试协调器扩展支持8/16参与rank；服务控制协议不变。

**通用主线功能源码：无改动。** 微测留在独立测试目录，因为其任务是创建人为负载和测量，而非提供服务运行行为；不需要Ascend服务hook。

## 脚本、测试与说明

脚本按调用顺序：

1. [run_bandwidth.sh](../../ascend-mempool-test/scripts/run_bandwidth.sh)、[run_bandwidth.py](../../ascend-mempool-test/scripts/run_bandwidth.py)：双机入口、参数/身份核对、进程管理、报告互收和自动收尾。
2. [bandwidth_worker.py](../../ascend-mempool-test/scripts/bandwidth_worker.py)：NPU加载、版本/拓扑记录、阶段编排、采样与背景竞争。
3. [bandwidth_mooncake.py](../../ascend-mempool-test/scripts/bandwidth_mooncake.py)：full时自动探测/启动独占Mooncake服务，远端get、真实缺测原因及三次汇总。
4. [bandwidth_summary.py](../../ascend-mempool-test/scripts/bandwidth_summary.py)：版本/覆盖核对、U6/Q24/Q27/Q7/Q5结论和Matplotlib SVG曲线。

测试：[test_bandwidth.py](../../ascend-mempool-test/tests/unit/test_bandwidth.py)四个计划/报告行为；
[test_bandwidth_runner.py](../../ascend-mempool-test/tests/unit/test_bandwidth_runner.py)成功与失败的真实本地双机模拟、报告收集和父进程清理。
说明：BANDWIDTH.md、本交付、[实施记录](ticket14-implementation-plan.md)、[实施前范围讨论](ticket14-scope.html)。
进度同步：[目标页D](../mempool-min/target-alignment.html)、memory与ticket14 Comments。

## 检查与证据

- 新增6个关键测试方法通过。4个计划/报告方法先失败后通过，不在末轮重复。
- 末轮运行588个方法：582通过，6个原有失败方法产生8个failure与5个error记录（含subtests）。
- 合计592个不同方法，586通过、6个原有失败；不能称全套通过。
- 原有失败在50395d0b3b源码复现：不再适用的配置拒绝断言、旧10000端口期望、已删除available_port接口。
- 基线复核先发现临时archive缺Ascend PD模块，五项未执行；补齐原代码后执行五项，没有重复已复现的配置测试。
- Ruff、7模块Linux目标strict mypy、bash语法检查通过。
- 目标HTML已重新生成；旧历史段落的7条STE写作警告保留，未改写原有资料。
- 使用明确标记的模拟数据检查六组SVG生成/XML；这是排版检查，不是实测曲线。
- [机器可读检查记录](verification/ticket14-20261105/checks.json)与同目录原始CPU日志保留失败证据。

## Standards

最终未解决项0。修复重复run-id及Matplotlib预检失败时覆盖旧报告的问题。
Mooncake复用同一percentile函数；双方对比p99统一合并三轮原始操作。

## Spec

最终未解决项0。增加D读无写基线与背景p99；先汇总同批写者的最晚完成，再对三次首操作取中位数。
Mooncake按切片大小输出三次中位数、统一p99比较表与曲线，原始样本单独保留。

## 验收边界

- 未访问NPU主机，未得到真实读写、并发、驻留或性能结果。
- remote-write只覆盖优先子项，full才覆盖全矩阵；smoke独立标识。
- 背景读窗口含控制边界等待；CPU核占用是进程CPU时间/墙钟时间。
- 层窗口比较是对齐首操作的传输可行性判断；78层连续提交无模型计算，不能宣称开销已隐藏。
- Mooncake get终点为CPU bytes，自身gather为NPU HBM；协议与端点差异会明确记录。
- 页驻留不可观测时保留unknown；没有给性能设统一达标线，也没有开启默认trace。
- 14须经用户NPU确认后关闭；25仍需14远端写证据及多基址kernel条件。
