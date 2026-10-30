# K1-ticket03：prefix split_read交付

整票基点`e23b57df7d`；合并缓冲修订基点`87e802de03`，本次005改造基点`6f50c4d54b`。
实现、本地检查与双轴复审已完成。005在19003 config store启动时报地址占用；
事后连接查询为0，本轮未进入prefix校验。用户现已确认整个prefix验证链不额外读BM。
V2改用计算时独立保存的CPU源快照，006待复测，ticket保持open。

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

验收参考在计算源产生时由capture_source独立保存到CPU；admit完成后按prefix页绑定保留。
每层gather后将汇总HBM取回CPU，按槽位与源快照逐token比较；之后执行scatter。
全部恢复event完成后，按allocator索引核对原生HBM与同一源快照，覆盖两类数据。
不再读取BM作为oracle，也没有额外诊断索引或第三块HBM数据缓冲。
alive/admit/retained/fill/suffix记录的是计算源、页绑定与生命周期，DRAM内容未直接验证。
所有prefix报告显式带content_source与bm_readback=false；oracle_read_bytes必须为0。
生产load_back、fill/admit访问和完成门控保留；临时四段计时仍用于验收后清理。

## 功能源码阅读顺序

Ascend/NPU专用代码：

1. [split_read.py](../../python/sglang/srt/hardware_backend/npu/mempool/split_read.py)：轮转/连续分块分配函数。
2. [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py)：两块汇总缓冲、共享槽位表、三步恢复与计算源快照核对。
3. [restore_kernel.py](../../python/sglang/srt/hardware_backend/npu/mempool/restore_kernel.py)：Triton-Ascend单次发射，直接散开到flat/FIA NZ及Index K。
4. [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py)：分配策略和临时计时配置。
5. [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：P侧接入TP设备组与配置；计算结束核对源捕获覆盖。
6. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：注册实际分配策略。
7. [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py)：核对策略；临时保存Main下发至ack的op2耗时。
8. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：全员设备完成后ack和因果证据。
9. [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py)：fill完成后核对prefix/suffix组合的源覆盖。
10. [restore_timing.py](../../python/sglang/srt/hardware_backend/npu/mempool/restore_timing.py)：临时NPU事件计时，明确待验收后删除。

manager.py已撤销本票全部净改动：恢复原创建标志，删除直接CPU视图，故不列入最终功能阅读清单。

通用主线功能源码：无改动。配置、模型接线和调度证据均可通过既有Ascend/NPU入口提供。

## 脚本、测试与文档

- `ascend-mempool-test/scripts/run_mempool_min.py`：新增`split-read-acceptance`，复用两轮编排，分别运行两策略。
- `ascend-mempool-test/scripts/verify_mempool_split_read.py`：读取量、oracle、ack时序与策略输出比较。
- `verify_mempool_prefix.py`、`verify_mempool_reuse.py`、`verify_mempool_full.py`、`verify_mempool_session.py`：区分源快照与HBM证据，报告DRAM内容覆盖限制。
- `test_prefix_split_read.py`：真实映射存储加CPU集合通信替身，核对恢复内容和实际复制流量。
- `test_mempool_restore_kernel.py`：执行真实核函数的地址算式，用CPU边界验证flat/NZ、512/64拆分及跳过补齐。
- `test_mempool_split_read_evidence.py`：三步/缓冲/槽位表证据反例、计时表和双角色CLI预览。
- `test_mempool_subrouter.py`：既有延迟最后rank用例接入分工读取，验证不提前ack。
- `test_prefix.py`、`test_pool.py`、`test_mempool_router.py`：扩展既有存储/注册fixture，阻止host映射与CPU解引用，核对真实V2读量和缓冲复用。
- `test_mempool_prefix_service.py`、`test_mempool_prefix_evidence.py`：源验证调用与报告来源标记；复用原有请求/生命周期边界。
- [逐机运行说明](../../ascend-mempool-test/K1_TICKET03.md)、本记录、票据Comments与目标对齐D节。

上述脚本和测试属于仓库约定的独立NPU验收目录，负责外部编排和CPU设备替身。
它们不属于SGLang公共主线功能；无需在生产Ascend hook中承载测试进程管理。

## 验证与下一步

最新改造见[005诊断与检查](verification/k1-ticket03-005-20261030/README.md)：
完整494项CPU单轮通过，8模块strict mypy及13文件静态检查通过，Standards/Spec均0项。
NPU005启动失败，006待用户执行；CPU结果不表示硬件验收通过。
此前[004诊断与检查](verification/k1-ticket03-004-20261030/README.md)记录的额外私有副本读取现已移除。
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
同时清理本轮新增的工程验收探针（缓冲/发射计数、首次恢复日志、gather源快照核对）；
私有副本读回函数与oracle索引已按用户要求删除，原有计算源快照用于当前内容验收。
保留功能核、槽位表、两块缓冲和完成门控。
验收脚本、原始报告、此清单继续保留；以后需要时从本commit恢复临时探针。
当前尚未验收，因此这些代码仍用于本次验证，不能提前删除。

四段计时的口径、历史基线与局限见[运行说明](../../ascend-mempool-test/K1_TICKET03.md)。
