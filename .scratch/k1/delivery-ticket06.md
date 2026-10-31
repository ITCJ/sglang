---
title: K1-ticket06：D主KV共享与每步顺序
subtitle: 实现交付与源码阅读顺序
template: doc
theme: blueprint
lang: zh
date: 2026-10-31
status: NPU001用户已验收；清理后510项CPU与双轴复审通过
---

**D主KV从16份变8份，每组成员分工写入，完整当前token从本rank的HBM供给。**
基点为`cc14b0518105921dd6320358f84cf912f28a9aeb`。
本票可独立于P共享启用；当前双机验收默认开启P/D两侧共享。
用户接受9b760a1add的NPU001两轮精简功能验收，ticket06已closed。

按授权清理本票工程插桩；本页源码清单为原实现加本次清理的最终净改动。
[回传核对与边界](verification/k1-ticket06-001-20261031/README.md)记录真实验证范围。

## A 数据与顺序

BM会话复用P/D成员分组。
D owner贡献一份主KV及成员探针，其他D成员贡献0并映射owner区域。
附属P区域继续独立，NUMA注册与物理页驻留分开记录。

每层计算出完整compact KV后，当前计算流提交`unidex_copy`。
默认两个rank各写288维，也支持按层轮转分工。
配置改变写者归属；不新增跨进程控制消息。

```flow LR
完整当前KV -> 本地HBM: 当前token供给
完整当前KV -> 分工写共享DRAM: 当前计算流
分工写共享DRAM -> 全员TP集合通信: 原有模型路径
全员TP集合通信 -> 下一步历史KV读取: 共享完整行已可见
```

当前代码原先允许新token的miss从DRAM读取。
共享分写后，该路径可能早于同组另一写者完成。
本票在runtime miss接口用本地完整KV供给当前token，并从BM miss掩码中移除它。
索引复制只触碰对应miss，避免覆盖并行hit stream的写入。

## B 同流与Graph依据

GLM-5.2的`GlmMoeDsaForCausalLM`继承`DeepseekV2ForCausalLM`。
现有`ascend_backend.py`在decode sparse attention之前调用`write_layer`。
`offload.py`直接调用当前流的`torch.ops.npu.unidex_copy`。

模型attention返回后进入既有`layer_communicator.prepare_mlp`。
TP16、DP1路径执行全员attention TP all-reduce或reduce-scatter。
两者都依赖全员参与；broadcast不作为本票顺序点。

生产写回保持同流，不需要新增写回event或通信。
既有materialize路径仍通过`copy_ready`和hit/miss事件协调流。
新读写操作在Graph捕获内使用设备索引及稳定HBM存储。
CPU不能证明NPU kernel捕获、HCCL顺序和跨设备DRAM可见性。

已验收版本逐层读回上一token整行，并用启动Graph探针验证延迟写流的等待。
清理后已删除上述额外DRAM读回、延迟matmul/event实验、设备计数及报告接线。
生产当前token缓冲和原有stream/event依赖保留，未改变默认退出。
NPU证据属于9b760a1add，清理后的版本仅有本机CPU验证。

## C Ascend/NPU功能源码

按配置、映射、执行和协调的顺序阅读：

| 文件 | 用途 |
|---|---|
| [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | D共享、分组、写策略与两侧副本数 |
| [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | P/D独立主KV共享，D owner贡献和区域归属 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 映射真实owner、跳过零贡献区域、成员探针与注册 |
| [offload.py](../../python/sglang/srt/hardware_backend/npu/mempool/offload.py) | 用UniDexCopy索引分写完整行的特征片段 |
| [decode_write.py](../../python/sglang/srt/hardware_backend/npu/mempool/decode_write.py) | 写分工、完整当前KV缓冲和本地miss供给 |
| [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 接入共享写回、本地当前token与原有完成检查 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | D共享描述符、双读者记账与NUMA接线 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 接受D共享成员不同的实际BM贡献 |

## D 通用主线与其他文件

通用主线功能源码净改动为0。
请求释放复用原有全rank drain/native-free门控。
服务退出沿用原有父进程清理路径。

| 脚本或说明 | 用途 |
|---|---|
| [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 共享D分配预算和通用回归编排；旧专用模式已撤下 |
| [verify_mempool_shared.py](../../ascend-mempool-test/scripts/verify_mempool_shared.py) | D区域、分配量与独立NUMA驻留证据 |
| [verify_mempool_decode.py](../../ascend-mempool-test/scripts/verify_mempool_decode.py) | 仅离线检查旧报告的逐步哨兵、Graph与写覆盖 |
| [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py) | 汇总当前功能回归，明确不再覆盖D顺序探针 |
| [verify_mempool_min.py](../../ascend-mempool-test/scripts/verify_mempool_min.py) | 模型BM miss核验；兼容旧报告的D哨兵统计 |
| [K1_TICKET06.md](../../ascend-mempool-test/K1_TICKET06.md) | 当前服务配置与9b760a1add历史验收命令 |

这些入口运行在独立测试进程，不能由worker内部hook替代。
测试位于`ascend-mempool-test/tests/unit/`。
池会话、共享decode内容、验收CLI、证据拒绝和最后读者释放均在既有公开接口验证。

| 测试文件 | 覆盖 |
|---|---|
| test_pool_session.py | D owner映射、P共享组合、零贡献与探针 |
| test_shared_decode.py | 两种分写、当前本地KV、历史完整行及正常完成记账 |
| test_mempool_session_acceptance.py | 两轮CLI与D实际分配预算 |
| test_mempool_shared_evidence.py | 注册、写入报告和NUMA证据拒绝 |
| test_mempool_subrouter.py | 等最后一名D读者停止后释放 |

## E 验证状态

池会话和CLI已通过红绿验证。
共享注册证据和延迟最后读者的CPU检查通过。
510项CPU覆盖最终收齐；完整回归仅运行一次，失败修复后定向补跑。

19个Python文件静态检查、14模块strict mypy通过。
Standards与Spec复审均为0项未解决问题。
[本机验证与次数说明](verification/ticket06-cpu-20261031/README.md)记录实际结果。

NPU001两轮各6请求已通过，当前features策略在真实设备上验证。
物理NUMA驻留和性能未验收；layers与P私有+D共享组合仅有CPU证据。
用户已确认并关闭06，09的对应依赖满足，仍等待08验收。

## F 验收后清理

删除decode_probe.py；runtime与decode_write移除逐步哨兵、设备计数、快照和报告字段。
协议删除verify_decode，transport撤掉D启动探针。通用主线功能文件仍为0个。

测试保留两种分写、当前本地读取、历史完整行和释放门控；撤掉已删除哨兵的损坏检测子场景。
[清理验证记录](verification/ticket06-cleanup-20261031/README.md)：完整CPU单轮510项、
11文件静态检查、13模块strict mypy通过；Standards/Spec各0项。
历史报告与旧专用命令均保留，不改写既有提交。
