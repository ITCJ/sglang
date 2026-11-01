---
title: K1-ticket07：P alive Index K 单份
subtitle: 实现、源码阅读顺序与验收边界
template: doc
theme: blueprint
lang: zh
date: 2026-11-01
status: 519项CPU覆盖与双轴复审通过；等待用户硬件验收
---

P alive Index K 从16份变1份，默认由rank0在NUMA 0写入。
P/D主KV仍各8份，prefix主KV与Index K仍各16份。
基点为1009c86788；本票仍使用索引拷贝，异步执行器留票11。

## A 存储与会话

现有BM初始化是进程级会话，原来按主KV共享组隔离。
为让所有P分片访问同一Index K，本票将底层会话扩为P/D共32个参与者。
P的16个分片共同映射Index K；主KV仍保留各自的两rank分组。
prefix各rank独立；映射数量与物理副本数分别计算。

rank0贡献Index K，其余rank不分配这一数据区。
默认布局的实际贡献预算为P 105 GiB、D 8 GiB。
物理NUMA页驻留依赖实机查询，不能由分配标记或CPU测试推断。

## B 写入、命中与释放

新算Index K默认仅rank0写回；可配置rank0/1按整页分写。
其余rank在普通运行中跳过Index K写回，不增加复制调用。
验收源快照仍取自已有计算结果。

fill由配置的Index K写者写共享区域；admit由全部P分片读取共享源。
各分片解析自己的地址，写入自己的prefix。
选页、地址解析和底层提交分开；协调者与账本不持有拷贝地址。

就绪位图分别跟踪主KV副本和唯一Index K区域。
写回及fill的Index K页完成前，不报告完整ready。
既有事件、全部分片admit回执、源保护和整槽释放继续控制生命周期。
主线消息、串行门控与默认父进程退出方式不变。

## C 功能源码阅读顺序

以下均为Ascend/NPU专用目录中的功能源码；通用主线功能改动为0。

| 文件 | 用途 |
|---|---|
| [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | 单份开关、Index K写者配置与副本数 |
| [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | 会话成员与物理副本分组、区域贡献及端口 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 共享区域解析、owner注册与原分组探针地址 |
| [page_write.py](../../python/sglang/srt/hardware_backend/npu/mempool/page_write.py) | 未分配Index K写任务的分片返回空页集 |
| [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | Index K写回分工、实际计数与启动接线 |
| [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | 单份fill、共享源admit和本地地址解析 |
| [mempool_accounting.py](../../python/sglang/srt/disaggregation/ascend/mempool_accounting.py) | Index K独立页覆盖与ready门控 |
| [mempool_tick.py](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py) | 本地事实携带实际Index K写入行数 |
| [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 原生row释放前后保留Index K写入事实 |
| [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 在有序tick汇总共享区域进度 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 写者、区域注册和分片配置接线 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 允许各参与者按实际所属区域贡献不同大小 |
| [shared_probe.py](../../python/sglang/srt/hardware_backend/npu/mempool/shared_probe.py) | 既有主KV哨兵继续按物理共享组核对 |

## D 测试、脚本与文档

| 文件 | 用途 |
|---|---|
| [K1_TICKET07.md](../../ascend-mempool-test/K1_TICKET07.md) | 每机一条验收命令、报告与覆盖边界 |
| [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | index-sharing-acceptance两轮编排、预算与清理 |
| [verify_mempool_shared.py](../../ascend-mempool-test/scripts/verify_mempool_shared.py) | 单份注册、写回及fill实际行数检查 |
| [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py) | 九请求证据和逻辑实验汇总 |
| [mempool_logical_cases.py](../../ascend-mempool-test/scripts/mempool_logical_cases.py) | 新增三题输入与独立答案 |
| [verify_mempool_reuse.py](../../ascend-mempool-test/scripts/verify_mempool_reuse.py) | 扩展核对新增请求的恢复和生命周期 |
| [test_pool_session.py](../../ascend-mempool-test/tests/unit/test_pool_session.py) | 16个P映射者共享Index K且主KV分组独立 |
| [test_shared_index.py](../../ascend-mempool-test/tests/unit/test_shared_index.py) | 写回、admit、fill内容与延迟写者就绪 |
| [test_mempool_shared_evidence.py](../../ascend-mempool-test/tests/unit/test_mempool_shared_evidence.py) | 拒绝第二owner、错误成员和NUMA声明 |
| [test_mempool_session_acceptance.py](../../ascend-mempool-test/tests/unit/test_mempool_session_acceptance.py) | CLI配置、预算与两轮约束 |
| [test_mempool_full.py](../../ascend-mempool-test/tests/unit/test_mempool_full.py) | 逻辑实验、错误答案与九请求证据 |
| [test_mempool_prefix_evidence.py](../../ascend-mempool-test/tests/unit/test_mempool_prefix_evidence.py) | 复用真实Main状态机生成九请求检查素材 |

执行文本和07/08/09/11已同步本地票序07→08→11→09。
07保持open，08仍等待07验收；11尚未实现。

## E 验证与精度边界

新池映射、共享内容、就绪和CLI接口按红绿步骤验证。
31项SR/服务/规划集成检查、17模块类型检查通过。
完整CPU单轮518项中，21项受沙箱权限阻断。
仅在沙箱外补跑这21项，全部通过；评审新增1项回归也已通过。

当前共519项CPU覆盖，Standards与Spec复审均无未解决问题。
[本机证据](verification/ticket07-cpu-20261101/README.md)与[双轴评审](verification/ticket07-cpu-20261101/review.md)已归档。

用户授权新增一组逻辑精度实验；每轮6个原请求加3道题，仍只启动两轮服务。
冷请求、改变答案的命中请求、完全命中分别有独立答案及命中长度检查。
失败保留证据，不自动增加BM读取。

用户重申不新增直接读BM验证。
内容证据继续使用计算源快照、正常load_back后的native HBM和实际写入计数。
CPU检查验证各份prefix的Index K内容；不冒充实机逐份BM读回。
硬件未运行，32参与者会话、物理NUMA、模型精度和真实释放仍待用户验收。
性能工具留测试分支，本票不声称已有性能结果。
