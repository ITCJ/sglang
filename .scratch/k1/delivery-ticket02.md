# K1-ticket02 交付：share=0 适配器与精简验收

本票基于 `993ae38ed6`，复用 NUMA 分配、共享分组和实验映射机制。
生产服务通过规划会话建池，仍为每 rank 私有副本。
状态为 **代码交付，等待用户 NPU 验收；State=open**。

[按NPU、Ascend、公共主线分组的最终代码清单](code-changes-ticket02.md)。

## 当前退出约定

用户明确要求沿用 SGLang 默认退出方法，由已有父进程/进程清理路径结束 worker，
并要求避免非必要的主线代码修改。已写入仓库 AGENTS.md，后续按此执行。

`scheduler.py`、`disaggregation/prefill.py`、`decode.py` 已恢复到本票之前的版本。
移除本票新增的主动 BM 关闭 hook、MR/SR quiesce、退出阶段日志及其测试。
验收入口恢复既有 `Processes` 清理自己启动的进程组；两侧自动协调，检查进程清理、
端口释放和同端口重启。父进程按默认方式终止 worker 不再视为本票失败。

不再要求逐 rank 的 `session_closed` / `distributed_released` 回执，
不把进程回收解释为已显式执行 BM leave/uninitialize。
独立池组件仍保留原有 `manager.close(drain)`，供组件调用者使用；真实服务不额外接入此关闭路径。
历史 NPU002/003 的失败记录保留，不改写为通过，也不据此关闭票据。

## 正常请求路径

规划器将 NUMA、owner、成员、participant、端口和贡献量组成 `PoolSession`。
服务在 Graph capture 前初始化 BM、建池并确认 device 映射。
实验 `SharingPool` 复用同一份 owner 映射和 join 检查，旧 rank-pair 包装已删除。

runtime 提供已核验各层主 KV 与 Index K 的写回事实；SR按副本记录命中段fill及新算段写回，
由覆盖位图计算 ready_tokens，与原连续计数器逐事件对照。
仍在整个 prompt 完成后发 ready，保留原三条件首 token 门控。

每份副本初始一个读者，D drain与写回结束后归零；P收到Main的free前，D-finished已证明远端读者停止。
free_ack仍要求native/source清理完成，保留逐rank零读者证据。

## 功能源码阅读顺序

1. [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py)：从既有分组生成会话参数。
2. [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py)：会话初始化、owner映射、注册与组件关闭接口。
3. [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：模型在capture前接入规划会话。
4. [mempool_accounting.py](../../python/sglang/srt/disaggregation/ascend/mempool_accounting.py)：副本覆盖位图与读者计数。
5. [mempool_tick.py](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py)：传递真实完成的写入范围。
6. [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py)：活跃和已分离请求的完成事实。
7. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：汇总覆盖与读者，发布ready/free证据。
8. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：注册副本owner、成员和会话。
9. [sharing_pool.py](../../ascend-mempool-test/src/ascend_mempool/sharing_pool.py)：实验组件复用公共适配器，移除重复join和owner映射。

验收入口看 [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py)
的两轮编排与 [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py) 的结果汇总。

## Ascend/NPU 目录外功能改动

本票在公共scheduler及PD循环上的改动全部撤回。
保留的目录外功能为 `ascend-mempool-test/` 独立NPU组件与验收脚本，按仓库约定存放于此；
运行编排、证据检查和SDK替身不放进生产包。

## 验收与精简

[逐机命令及判据](../../ascend-mempool-test/K1_TICKET02.md)。
`session-acceptance`固定两轮：首轮请求后注入故障，第二轮验证同端口重启；每轮使用既有进程清理。
复制smoke只运行block_dim 48、一次eager与一次replay。
V3使用独立标准答案，不重启参考服务；V8用真实注册做CPU容量拒绝重放。
报告明确标为 K1-02 reduced functional acceptance。
物理DRAM页计数与性能不在本票验收范围内。

历史实现检查见[初次验证](verification/ticket02-cpu-20261029/README.md)与
[已撤回停机扩展的验证](verification/ticket02-quiesce-cpu-20261029/README.md)。
最新[恢复默认退出验证](verification/ticket02-default-exit-20261030/README.md)：
483项CPU、6模块strict mypy及两轴复审通过；本次NPU未执行。
