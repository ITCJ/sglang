---
title: K1-ticket06：D主KV共享与每步顺序
subtitle: 实现交付与源码阅读顺序
template: doc
theme: blueprint
lang: zh
date: 2026-10-31
status: 本机验证与双轴评审通过；等待用户NPU验收
---

**D主KV从16份变8份，每组成员分工写入，完整当前token从本rank的HBM供给。**
基点为`cc14b0518105921dd6320358f84cf912f28a9aeb`。
本票可独立于P共享启用；当前双机验收默认开启P/D两侧共享。
NPU尚未运行，ticket06保持open。

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

验收逐层读回上一token整行，不依赖top-k选择。
启动探针单独模拟延迟写流，验证去掉等待失败、加等待通过。
两种版本均执行Graph replay，不运行性能计时。

## C Ascend/NPU功能源码

按配置、映射、执行和协调的顺序阅读：

| 文件 | 用途 |
|---|---|
| [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | D共享、分组、写策略、验收开关与两侧副本数 |
| [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | P/D独立主KV共享，D owner贡献和区域归属 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 映射真实owner、跳过零贡献区域、成员探针与注册 |
| [offload.py](../../python/sglang/srt/hardware_backend/npu/mempool/offload.py) | 用UniDexCopy索引分写完整行的特征片段 |
| [decode_write.py](../../python/sglang/srt/hardware_backend/npu/mempool/decode_write.py) | 写分工、当前KV缓冲、逐步哨兵和设备计数 |
| [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 接入共享写回、本地当前token、完成检查与报告 |
| [decode_probe.py](../../python/sglang/srt/hardware_backend/npu/mempool/decode_probe.py) | 验收开关下运行Graph延迟写者负测试 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | D共享描述符、双读者记账、NUMA及探针接线 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 接受D共享成员不同的实际BM贡献 |

## D 通用主线与其他文件

通用主线功能源码净改动为0。
请求释放复用原有全rank drain/native-free门控。
服务退出沿用原有父进程清理路径。

| 脚本或说明 | 用途 |
|---|---|
| [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | d-sharing-acceptance及两轮自运行编排 |
| [verify_mempool_shared.py](../../ascend-mempool-test/scripts/verify_mempool_shared.py) | D区域、分配量与启动Graph顺序证据 |
| [verify_mempool_decode.py](../../ascend-mempool-test/scripts/verify_mempool_decode.py) | 逐请求写覆盖、上一行校验、Graph和共享页描述符 |
| [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py) | 汇总本票与原有功能验收 |
| [verify_mempool_min.py](../../ascend-mempool-test/scripts/verify_mempool_min.py) | 分别记录模型BM miss与D哨兵读回 |
| [K1_TICKET06.md](../../ascend-mempool-test/K1_TICKET06.md) | 环境、逐机命令、失败条件和回传文件 |

这些入口运行在独立测试进程，不能由worker内部hook替代。
测试位于`ascend-mempool-test/tests/unit/`。
池会话、共享decode内容、验收CLI、证据拒绝和最后读者释放均在既有公开接口验证。

| 测试文件 | 覆盖 |
|---|---|
| test_pool_session.py | D owner映射、P共享组合、零贡献与探针 |
| test_shared_decode.py | 两种分写、当前本地KV、历史完整行及损坏拒绝 |
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

NPU入口采用两轮精简功能验收，具体覆盖边界见运行说明。
用户确认实现及自身NPU结果后，才关闭06并满足09的对应依赖。
