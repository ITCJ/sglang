# K1 验收后工程清理

用户于2026-11-03授权清理刚刚检查出的工程测试残留，并要求独立提交一个commit。
基点为 `b3bb63e067`。本次仅清理K1临时探针与性能插桩。

## 范围与正常路径

删除P共享启动跨写者探针、D启动Graph延迟正负测试、D每步上一token与末行额外读回、
设备校验计数、快照、完成回执字段和 `verify_decode` 配置。
删除pool_copy人工延迟配置与sleep、SDK提交/等待计时、poll次数/间隔统计及临时清单日志。
独立demo的SDK计时移到测试脚本的MeasuredHandle边界，不再由服务适配器采样。

共享分写、当前token的完整本地缓冲、历史miss读取、图中的stream/event、
库batch/wait、超时与UNKNOWN锁存、源保护及正常释放后retire继续沿用。
源快照、正常恢复后的内容比较、prefix证据文件属于既有正确性校验，继续保留。
运行握手的会话身份标记、物理NUMA证据采样及阶段0容量故障注入不属于本次删除范围。
默认退出继续走原父进程清理路径。

## 当前与历史入口

四组入口继续覆盖B=64/128、S_D=128/512、5120-token prefix、共享注册及page first，
并运行原有ABCE、长生成、容量拒绝、超时、答案/内容和双端清理回归。
报告 `k1.omitted_checks` 明确标注不再覆盖启动跨写者、逐步D哨兵/Graph延迟负测试、
人工延迟与搬运计时/poll证据，不能用旧报告字段虚构覆盖。
旧11服务入口改为仅离线核验历史日志；旧D报告核验器同样只用于历史报告。
清理前完整探针验收在 `b3bb63e067` 的独立checkout复现。
01、02、03、05、06、07、08、09、11、12继续closed，04、10继续open。
用户已接受的硬件结论只属于清理前版本；本次没有重跑或宣称清理后NPU验收通过。

## 功能源码阅读顺序

全部路径前缀为 `python/sglang/srt/`。

Ascend/NPU专用：

1. `disaggregation/ascend/mempool_router_protocol.py`：删除D探针与搬运延迟配置及签名字段。
2. `disaggregation/ascend/mempool_router_transport.py`：删除共享探针创建及注册接线。
3. `disaggregation/ascend/mempool_subrouter.py`：删除注册验证回调及shared_probe汇总。
4. `hardware_backend/npu/mempool/decode_write.py`：删除临时校验，保留完整当前token本地供给。
5. `hardware_backend/npu/mempool/runtime.py`：删除D校验装配、计数快照和额外完成字段。
6. `hardware_backend/npu/mempool/pool_copy.py`：执行器只保留任务提交、实际完成与超时状态。
7. `hardware_backend/npu/mempool/prefix.py`：删除pool-copy临时清单/计时日志，继续按真实完成出receipt。
8. 删除 `hardware_backend/npu/mempool/shared_probe.py` 和 `decode_probe.py` 两个验收专用模块。

通用主线功能源码净改动为0。

## 脚本、测试与文档

- `scripts/run_k1_full_acceptance.py`、`run_mempool_min.py`：当前配置和报告适配，保留四组几何组件校验。
- `scripts/verify_mempool_shared.py`：注册检查不再依赖已删除的启动探针。
- `scripts/run_pool_copy_acceptance.py`、`verify_mempool_decode.py`：离线保留历史日志校验。
- `scripts/verify_pool_sdma.py`：独立demo自己测量SDK调用，继续复用生产batch/wait适配器。
- `tests/unit/test_pool_copy.py`：保留完成/超时/故障/空任务与绑核回归，增加测试侧测量边界核对。
- `test_shared_decode.py`：保留两种分写、跨页、当前token本地供给、历史完整行和完成记账。
- `test_k1_full_acceptance.py`、`test_mempool_shared_evidence.py`：同步当前配置/注册契约，保留历史报告负例。
- `test_mempool_subrouter.py`：删除仅针对已撤销注册探针的异常用例；正常完成/回收门控测试保留。
- `K1_TICKET05/09/11.md`、本记录、memory及目标页D：同步当前入口和历史证据边界。

以上scripts和tests路径均位于 `ascend-mempool-test/`，属于独立验证工具，不是通用功能源码。

## 本机验证

- 第一轮完整CPU：556项中550个方法通过；6个方法产生8 failures、5 errors（含子场景）。
- 在隔离目录用 `git archive b3bb63e067` 恢复原始源码，仅运行这6个方法，复现完全相同的13条失败/错误记录。
  归属为旧UINT32_MAX预期、已移除available_port的mock，以及端口10000未纳入旧预期。
  本次未修改这些基线测试与对应功能。
- 第二轮定向CPU：59项全部通过，覆盖本次修改的共享D数据/注册、Main/SR门控、prefix拷贝、
  pool-copy完成/超时/故障、历史核验器、四组配置及测试侧SDK测量。
- 18个Python文件ruff、格式、isort、Python3.9语法检查通过。
- 11个相关模块Linux目标strict mypy通过；首跑Mac目标/缺少类型依赖造成4条诊断，
  采用部署目标并纳入已有类型依赖后通过，详见原始静态日志。
- 两轴只读复核：Standards 0项；Spec指出报告标题仍称完整验收，已修正为清理后的功能回归。
  最终复核结论见独立review记录。
- 没有访问或重跑NPU；清理前验收不扩大为清理后硬件通过。

完整日志与统计见 [CPU及基线记录](verification/cleanup-test-hooks-20261103/checks.json)、
[首轮CPU](verification/cleanup-test-hooks-20261103/cpu.log)、
[原版本复现](verification/cleanup-test-hooks-20261103/baseline.log)、
[第二轮定向](verification/cleanup-test-hooks-20261103/focused.log)、
[静态检查](verification/cleanup-test-hooks-20261103/static.log)。

覆盖减少：撤销所有启动跨写者/Graph延迟负测试、逐步与末行额外哨兵和搬运性能证据。
删除1项仅针对注册探针的异常测试、P探针报告损坏子场景及共享D哨兵计数断言；
普通数据内容、mask/补齐、完整历史行、超时fail-stop与最后分片回收门控继续验证。
