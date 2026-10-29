# K1-ticket02 交付：share=0 适配器与精简验收

本票基于 `993ae38ed6`，复用 NUMA 分配、共享分组和实验映射机制。
生产服务通过规划会话建池，仍为每 rank 私有副本。
状态为 **代码交付，等待用户 NPU 验收；State=open**。

## NPU002 关闭修正

用户运行 NPU002 时，D 断连使仍在工作的 Main 向 P 传播 `Host unreachable`。
本次改为先通过 Main 的 `POST /quiesce` 停止接单，已有请求仍完成原释放流程。
双方 free_ack 收齐后，Main 请求两侧 SR 确认全部 rank 空闲。
32 个 rank 的确认齐全后，SR 停止控制 tick，Main 停止向已确认的 SR 发心跳。
验收入口记录 `control-quiesced.json`，跨机确认后才先关闭 D、再关闭 P。
意外断连仍然 fail-stop；真实 TCP/ZMQ 回归覆盖受控与意外两种断连。

正常退出报告同时要求逐 rank 的 BM 关闭与 `distributed_released` 回执，
并拒绝 scheduler 强制清理或崩溃，避免只凭父进程返回0误判。
新增 `host_resources_released` / `distributed_released` 标记用于定位 BM 之后的退出阶段。
NPU002 的 D 后续滞留及 native double free 尚无调用栈，未宣称已修复；等待 NPU003 验证。

## 正常路径

规划器将 NUMA、owner、成员、participant、端口和物理贡献组成 `PoolSession`。
服务在 Graph capture 前初始化 BM、建池并确认 device 映射。
实验 `SharingPool` 复用同一份 owner 映射和 join 检查。
旧 `initialize_rank_pair()` 包装已删除，启动测试也迁到实际服务接口。

写回完成事实由 runtime 提供；该事实已经核验各层主 KV 和 Index K。
SR 分别记录命中段 fill、新算段写回，按副本覆盖位图算公共连续范围。
旧计数器同时计算，验收逐事件比对结果，保留原连续写入约束。
本票仍在整个 prompt 完成后发 ready，三条件首 token 门控保持原语义。

每份副本初始一个读者；D 在 drain 和写回结束后归零。
P 收到 Main 的 free 时，D-finished 已证明远端读者停止。
free_ack 还需 native/source 清理完成，报告记录每 rank 的零读者证据。

正常退出沿用 scheduler 的 graceful 资源释放入口，先同步设备，再 leave、destroy、
BM uninitialize；保持共享的全局 MF 给 TransferEngine，直到进程退出。
精简验收先完成 Main/SR 停机确认，再退出 D 客户端，最后退出持有 BM Store 的 P。
故障路径仍保留分配到进程退出；不把故障进程终止解释为成功 drain。

## 功能源码阅读顺序

1. [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py)：从既有分组生成会话参数。
2. [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py)：多成员会话初始化、owner 映射、注册与关闭。
3. [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)：真实模型在 capture 前接入规划会话。
4. [mempool_accounting.py](../../python/sglang/srt/disaggregation/ascend/mempool_accounting.py)：副本覆盖位图与读者计数。
5. [mempool_tick.py](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py)：传递真实完成的写入范围。
6. [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py)：活跃/已分离请求的完成事实，以及正常退出清理。
7. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：在调度 tick 中汇总位图与读者，发布 ready/free 证据。
8. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：注册副本 owner、成员和会话。
9. [prefill.py](../../python/sglang/srt/disaggregation/prefill.py)、[decode.py](../../python/sglang/srt/disaggregation/decode.py)：让四种 PD 调度循环响应既有 graceful exit。
10. [scheduler.py](../../python/sglang/srt/managers/scheduler.py)：在正常资源释放时调用 Ascend 服务关闭。
11. [sharing_pool.py](../../ascend-mempool-test/src/ascend_mempool/sharing_pool.py)：让原共享组件复用公共适配器，移除重复的 join 和 owner 映射。

本轮关闭修正的阅读顺序：

1. [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py)：停止接单、等待 free_ack、收齐全 rank 空闲回执。
2. [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py)：quiesce HTTP 入口与停止已确认 SR 的心跳。
3. [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py)：TP 共识确认空闲，回执后停止协议处理。
4. [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py)：同时停止旧 P/D 控制 tick，记录结构化退出阶段。
5. [scheduler.py](../../python/sglang/srt/managers/scheduler.py)：在主机资源释放、通信清理完成后发出阶段回执。

验收入口依次看 [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py)
的两轮编排、[verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py)
的证据汇总，以及 [run_service.sh](../../ascend-mempool-test/scripts/run_service.sh) 的服务进程退出路径。

## Ascend/NPU 目录外功能改动的必要性

- `scheduler.py`：接入退出 hook 与主机/通信清理完成回执。公共进程 finally 才知道
  何时进入正常退出，以及其他资源释放和 `abort_distributed_environment()` 何时返回；
  Ascend 服务自身无法观察调用方后续清理，也不能用 atexit 保证设备和跨进程访问已停止。
- `disaggregation/prefill.py`、`decode.py`：原四个 PD 循环为 `while True`，不响应
  `ShutdownReq` 设置的 `gracefully_exit`，导致新关闭 hook 不可达。
  改成与普通 scheduler 相同的退出条件；Ascend hook 不能退出调用方的循环。
- `ascend-mempool-test/`：独立 NPU 验收与原共享组件按仓库要求存放在这里。
  运行脚本、证据核对和 SDK 替身不放进生产包；旧共享组件删除重复实现。

## 验收与精简

[逐机命令及判据](../../ascend-mempool-test/K1_TICKET02.md)。
`session-acceptance` 固定两轮；首轮正常请求后注入故障，第二轮验证重启与正常 BM 关闭。
复制 smoke 改为服务使用的 block_dim 48、一次 eager 与一次 replay。
旧完整验收入口保留用于后续确需全覆盖的任务，K1-02 默认使用精简入口。

用户授权精简后，V3 改为独立标准答案检查；不重启参考服务。
V8 容量检查用实际注册做 CPU 重放；不另启容量故障服务。
报告明确标为 K1-02 reduced functional acceptance，保留这些覆盖变化。
性能仍在测试分支；真实物理页落点和故障退出后 DRAM 页计数未由本票证明。

## Mac 验证与审查

完整 CPU 套件及审查修复后的结果见 [验证记录](verification/ticket02-cpu-20261029/README.md)。
TDD red/green 覆盖规划会话、SR 页覆盖、正常关闭和证据缺失。
审查发现的旧包装、IPv6、TIME_WAIT、Store 退出顺序和 PD 循环退出问题均已修复。
Standards 与 Spec 分别复审，无剩余阻塞；本次停机修正尚未执行 NPU。
本轮完整 CPU 490 项、6 模块严格类型检查通过，见[停机修复验证](verification/ticket02-quiesce-cpu-20261029/README.md)。
