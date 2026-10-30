# K1-ticket03：prefix split_read交付与清理

整票基点`e23b57df7d`；用户验收版本`0337b9b9adb48baf160495594ec9b28908e890ba`。
用户在2026-10-30确认31/32的`k1-ticket03-006`双策略功能验收通过，03已closed，
并授权清理临时测试代码。见[人工确认](verification/k1-ticket03-006-20261030/acceptance.md)
及[完整结果与范围](verification/k1-ticket03-006-20261030/README.md)。

临时探针已删除，清理后CPU与复审进度见
[清理记录](verification/ticket03-cleanup-20261030/README.md)。
006硬件结果对应清理前版本；清理后未重新运行NPU。
直接DRAM内容、物理NUMA及生产性能等已约定的验收限制保留。

## 清理后的正常路径与所有权

Main下发原有load_back，SR在统一调度tick内执行。
分配函数按逻辑页分工；每rank从自己的物理prefix副本读取分到的页。
每次恢复分配主KV和Index K两块汇总缓冲，层间复用。
每层H2D直接写本rank区段，再in-place all_gather，最后单核索引散开。
散开核直接完成flat/FIA NZ地址映射、主KV的512/64拆分和Index K整行写入。

槽位表每次恢复建立一次，所有层及两类数据共用；补齐槽位不生成有效表项。
B/C各用32个槽位：主KV4,718,592 B，Index K1,048,576 B，共5.5 MiB。
C命中31页，rank15第二槽位为补齐；没有额外数据暂存区或重排缓冲。
操作持有索引、表和缓冲，直到恢复event完成后才释放引用。

全部恢复提交后记录完成event；poll只查询event并返回缓存回执。
SR复用原有全rank汇总，收齐设备完成回执后向Main ack；后缀等整段恢复完成。
prefix副本、fill/admit、默认父进程清理沿用已验收行为。

原有verify_prefix选项保留：计算时独立保存CPU源快照，admit后按prefix页保留引用。
恢复完成后核对原生HBM与快照，覆盖主KV及Index K。
alive/admit/retained/fill/suffix记录计算源、页绑定与生命周期，明确bm_readback=false。
原生内容检查不读取BM作oracle；默认普通服务不开启该验证。
每层gather后的额外HBM→CPU比较已移除。

## 最终净功能源码阅读顺序

以下按整票基点至清理后工作树的净改动列出。

Ascend/NPU专用代码：

1. [split_read.py](../../python/sglang/srt/hardware_backend/npu/mempool/split_read.py)：轮转/连续分块分配。
2. [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py)：两块缓冲、共享槽位表、三步恢复；计算源与native HBM核对。
3. [restore_kernel.py](../../python/sglang/srt/hardware_backend/npu/mempool/restore_kernel.py)：单次散开到flat/FIA NZ及Index K；无计时参数或预编译探针。
4. [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py)：分配策略配置及合法值约束。
5. [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：P侧接入TP设备组、策略与源捕获覆盖检查。
6. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：注册实际分配策略。
7. [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py)：核对注册策略一致。
8. [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py)：fill完成后核对prefix/suffix组合的源覆盖。

通用主线功能源码：无净改动。以上行为均由Ascend/NPU入口提供。
manager.py、mempool_subrouter.py相对整票基点已无净改动；restore_timing.py已删除。

## 脚本、测试与文档

- run_ticket10.py与test_acceptance_runner.py：保留NPU002暴露的HTTP BadStatusLine有界就绪重试修复。
- verify_mempool_prefix/reuse/full/session.py：保留计算源与原生HBM的证据来源、精简覆盖及DRAM未直接验证说明。
- verify_mempool_split_read.py及test_mempool_split_read_evidence.py：保留已验收版本的离线报告核验、计时表和反例。
- test_prefix_split_read.py：实际复制边界计数、两策略、1/31/32页、非连续页、两块缓冲、一张表、补齐及内容核对。
- test_mempool_restore_kernel.py：真实散开核地址表达式的CPU验证，覆盖flat/NZ、512/64、Index K及补齐跳过。
- test_mempool_subrouter.py：保留真实恢复路径的最后rank延迟回执检查。
- test_prefix.py、test_mempool_prefix_service/evidence.py、test_pool.py、test_mempool_router.py：源快照、原生恢复、页生命周期、注册及证据范围检查。
- [运行说明](../../ascend-mempool-test/K1_TICKET03.md)、票据Comments、验证归档及目标对齐D节/HTML。

这些脚本和测试属于仓库独立验收目录，不属于通用主线功能源码。
run_mempool_min.py撤下专用split-read-acceptance后恢复整票基点内容；其他服务和验收模式保留。
历史双策略入口可在0337b9b9ad复现，当前离线脚本仍核对旧证据，不放宽通过判据。

## 已清理的临时代码

- restore_timing.py及PrefixStore/runtime/RouterConfig/Main的profile_prefix接线。
- scatter包装的计时参数、warmup编译/加载探针；正常JIT发射仍由Triton完成。
- _Read中的split_read/timing证据状态、每层发射和读取字节计数、两块缓冲首次日志。
- slot_pages诊断副本、逐层gather额外比较及对应verified字段。
- 完成/ack时间戳日志和Main的op2诊断墙钟。
- 依赖上述探针的当前专用启动模式，以及只检查旧入口和计时编译顺序的2项测试。

实际复制、槽位表、两块缓冲、恢复stream、event及全rank完成门控保留。
现有CPU回归通过设备替身观察真实内容/流量，不依赖已删除的生产证据字段。
baselinetest未改；历史失败和NPU006原始报告保留。

## 验证记录

- [005源快照改造](verification/k1-ticket03-005-20261030/README.md)：494项CPU、静态检查和双轴复审。
- [006 NPU结果](verification/k1-ticket03-006-20261030/README.md)：两策略内容/流量/输出、默认清理与重启通过，含诊断计时及限制。
- [用户确认](verification/k1-ticket03-006-20261030/acceptance.md)：确认功能验收，关闭票据，授权清理。
- [清理后检查](verification/ticket03-cleanup-20261030/README.md)：独立记录本机检查与两轴复审；不等同新增NPU实测。
