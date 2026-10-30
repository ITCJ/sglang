# K1-ticket03：prefix split_read交付

整票基点`e23b57df7d`；合并缓冲修订基点`87e802de03`，本次004修复基点`3319ceb83d`。
实现、本地检查与双轴复审已完成。NPU003在B的私有副本CPU比较时SIGSEGV；
004请求CPU可读映射又触发HalMemSetAccess 65534，现已撤销该路径；
V2改为复用恢复缓冲经NPU读取实际私有副本，005待复测，ticket保持open。

## 正常路径与所有权

Main下发原有`load_back`，SR在统一调度tick内执行。
分配函数按命中页的逻辑顺序分工；每rank从自己的物理副本读取分到的页。
每次恢复只分配主KV和Index K两块汇总缓冲，层间复用。
每层先H2D直接写本rank区段，再in-place all_gather，最后单核索引散开。
散开核一次完成原生flat/FIA NZ地址映射及512/64拆分；Index K整行写入。
槽位表每次恢复建立一次，所有层及两类数据共用，补齐槽位不生成表项。
B/C各用32个槽位：主KV4,718,592B，Index K1,048,576B，共5.5MiB。
C命中31页，rank15第二个槽位为补齐；不存在独立暂存区或KV重排缓冲。
prefix副本、fill和admit的所有权及操作不变。

本地event记录在全部恢复提交之后。生产poll查询event并返回缓存回执，不重复集合通信。
SR复用已有全rank结果汇总，收齐所有设备完成回执后才向Main ack。
后缀计算仍等整段恢复完成。进程退出沿用SGLang默认父进程清理。

验收模式在gather后保存CPU快照，scatter读取完输入后，再复用同一汇总缓冲，
通过NPU读取私有prefix并取回CPU，与快照逐token比较。
全部恢复完成后再次复用该缓冲读取私有prefix，核对保存来源和原生HBM。
额外诊断索引每次恢复创建一次，跨类型/层复用；没有第三块HBM数据缓冲。
散开完成后在CPU核对原生HBM内容、私有prefix及保存来源，覆盖两类数据。
原有保存来源、原生恢复及fill的内容检查同时保留。
验收oracle读取量独立记账；本次另接入临时四段计时，验收后清理。

## 功能源码阅读顺序

Ascend/NPU专用代码：

1. [split_read.py](../../python/sglang/srt/hardware_backend/npu/mempool/split_read.py)：轮转/连续分块分配函数。
2. [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py)：两块汇总缓冲、共享槽位表、三步恢复与精确oracle。
3. [restore_kernel.py](../../python/sglang/srt/hardware_backend/npu/mempool/restore_kernel.py)：Triton-Ascend单次发射，直接散开到flat/FIA NZ及Index K。
4. [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py)：分配策略和临时计时配置。
5. [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：P侧接入TP设备组与配置。
6. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：注册实际分配策略。
7. [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py)：核对策略；临时保存Main下发至ack的op2耗时。
8. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：全员设备完成后ack和因果证据。
9. [restore_timing.py](../../python/sglang/srt/hardware_backend/npu/mempool/restore_timing.py)：临时NPU事件计时，明确待验收后删除。

manager.py已撤销本票全部净改动：恢复原创建标志，删除直接CPU视图，故不列入最终功能阅读清单。

通用主线功能源码：无改动。配置、模型接线和调度证据均可通过既有Ascend/NPU入口提供。

## 脚本、测试与文档

- `ascend-mempool-test/scripts/run_mempool_min.py`：新增`split-read-acceptance`，复用两轮编排，分别运行两策略。
- `ascend-mempool-test/scripts/verify_mempool_split_read.py`：读取量、oracle、ack时序与策略输出比较。
- `test_prefix_split_read.py`：真实映射存储加CPU集合通信替身，核对恢复内容和实际复制流量。
- `test_mempool_restore_kernel.py`：执行真实核函数的地址算式，用CPU边界验证flat/NZ、512/64拆分及跳过补齐。
- `test_mempool_split_read_evidence.py`：三步/缓冲/槽位表证据反例、计时表和双角色CLI预览。
- `test_mempool_subrouter.py`：既有延迟最后rank用例接入分工读取，验证不提前ack。
- `test_prefix.py`、`test_pool.py`、`test_mempool_router.py`：扩展既有存储/注册fixture，阻止host映射与CPU解引用，核对真实V2读量和缓冲复用。
- [逐机运行说明](../../ascend-mempool-test/K1_TICKET03.md)、本记录、票据Comments与目标对齐D节。

上述脚本和测试属于仓库约定的独立NPU验收目录，负责外部编排和CPU设备替身。
它们不属于SGLang公共主线功能；无需在生产Ascend hook中承载测试进程管理。

## 验证与下一步

最新修复见[004诊断与检查](verification/k1-ticket03-004-20261030/README.md)：
完整494项CPU单轮通过，7模块strict mypy及静态检查通过，Standards/Spec均0项。
NPU005待用户执行；CPU结果不表示硬件验收通过。
此前[003修复检查](verification/k1-ticket03-003-20261030/README.md)的495项CPU通过，
但host映射路径已被NPU004证伪并撤销。
[合并缓冲本机验证](verification/ticket03-combined-cpu-20261030/README.md)记录此前检查。
该历史版本完整CPU491项通过，10模块strict mypy通过，Standards/Spec无剩余发现。
NPU003在首次scatter之前的CPU校验崩溃，尚无散开核执行通过证据。
CPU地址语义验证不替代Triton-Ascend编译、HCCL输入输出别名和恢复stream实测。
旧四跳版本的[验证记录](verification/ticket03-cpu-20261030/README.md)仅作历史保留。

## 临时测试代码的删除边界

用户明确本轮测试可放在当前commit，baselinetest分支暂不修改。
用户确认NPU验收后，删除`restore_timing.py`及PrefixStore、runtime、RouterConfig、Main中的计时接线。
同时清理本轮新增的工程验收探针（缓冲/发射计数、首次恢复日志、私有副本验证接线）；
用于私有副本验收的_read_private_prefix、oracle索引和CPU快照接线一并删除。
保留功能核、槽位表、两块缓冲和完成门控。
验收脚本、原始报告、此清单继续保留；以后需要时从本commit恢复临时探针。
当前尚未验收，因此这些代码仍用于本次验证，不能提前删除。

四段计时的口径、历史基线与局限见[运行说明](../../ascend-mempool-test/K1_TICKET03.md)。
