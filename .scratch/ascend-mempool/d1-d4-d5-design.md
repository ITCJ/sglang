# TP control tick、drain 与 fault 接入草案

日期：2026-09-29。状态：D1、D4 首版 drain 策略和 D5 直接报错终止策略已确认；已同步至 spec。故障检测与退出接线仍须实现时核对。
范围：02③④接线设计。01、02①②已有实现；本轮不修改运行代码。

## D1：同侧统一推进

P16、D16 各自使用完整 TP CPU group；不建立跨 P/D 的32-rank collective。
固定 demo 验证 TP16 / DP1 / CP1 / PP1。P slot 与 D slot 独立选择。

在 P/D normal、overlap 四条循环的 ingest_requests 后、paused 判断前执行 tick。
后续结果回调产生的事件进入 pending observations，在下一次 tick 处理。
有 mempool pending/active work 时不能仅凭普通队列为空进入长时间 idle sleep。

输入：

- 按逻辑 `(room, attempt)` 聚合的网络消息；attempt 由 D 统一分配并分发。
- 本地 phase、binding、free slots、已验证的消息观察结果。
- P writes complete、D quiesced/drained、原 transfer readiness。
- 请求 cancel、共享 bootstrap deadline 到期，以及 local fatal fault。

session/proof 在各 pair 内校验，不跨 TP 比较完整 RequestIdentity。
消息到达不同步是正常等待；观察结果须保留到能够统一推进，不能只比较本轮新消息。

D1 首版固定流程：

1. 收集本地 observations，不在接收线程或回调中改变 ownership。
2. 固定 snapshot all-gather，得到相同的候选 key 集合和排序。
3. 根据相同输入构造 plan，并汇总本地 preflight 成功/fault。
4. 无 fault 时执行统一 plan；捕获本地执行异常。
5. 固定汇总执行状态。全部成功才发出 outbox 消息并允许模型调度。

collective 数量/顺序不依赖本地消息数。空输入也参与。
提交失败不尝试继续服务；具体停机由 D5 负责。本流程不是跨进程故障下的原子事务。

所有 ownership 变化都由 tick 统一批准，包括 acquire、普通 safe rollback、
finish_drain、apply(DONE)，以及会消费 pending_done 的 finish_prefill_writes。
仅同步 acquire 不能维持相同 free set。

正常 release 顺序：

- D：所有 rank 停止该请求的新提交并完成 drain → 清理旧 binding 引用 →
  同步提交 D slot release → 全部成功后发送各自 DONE。
- P：所有 rank 收到各自精确 binding 的 DONE，且全部 P writes 完成 →
  同步提交 P slot release → 全部成功后发送各自 RELEASE_ACK。
- D：收到全部对应 ACK 后关闭该逻辑 attempt；D slot 已可在 drain release 后复用。
- 同一 tick 可在 release 成功后 acquire，但出站旧 release 消息先于新 acquire 消息。

优先级：fatal 阻止正常推进；cancel/timeout 阻止同一 attempt 的新工作；
安全 release 先于新 acquire。历史消息的幂等回复不应重新改变 ownership。

容量不足继续 waiting，沿用请求级 bootstrap deadline，不因重试重置。
不能只复用 sender init_time：acquire 可能早于 sender 创建，或晚于普通 bootstrap 成功。
超时决定由 leader 统一发布，避免各 rank 独立时钟造成不同决策。

用户已确认：统一 acquire/release；一致 preflight 后意外部分 acquire 失败采用 fail-stop；
首版接受每 tick 的 CPU 元数据 collective 成本。正常容量不足仍等待，普通 cancel
仍允许安全 rollback。tick 随 scheduler 循环重复，不是固定时间重置；已有状态持续保留。

2026-09-30 再确认：保留上述 snapshot all-gather/preflight/commit 和空输入同步，
不采用本轮外部 review 的 MIN-only 或跳过空 tick 优化，demo 后再评估。

### P/D 准入 gate（2026-09-30 已确认）

P：在 `finalize_bootstrap()` 的 metadata 分配、sender 初始化等副作用之前，
检查统一 tick 已批准的 P acquire/binding 状态；未批准返回 False，继续等待。
该 hook 只读取批准结果，不自行 acquire 或修改 ownership。保持 optimistic prefill
关闭，并检查其他 finalize 调用入口，避免绕过 gate。

D：保留原 transfer、metadata 和 staging 推进；只有原路径就绪与 mempool KV_READY
均由 tick 确认后才允许离开 transfer queue。必须覆盖 `_poll_with_metadata_gate()`
和 `_poll_with_staging()`，等待 mempool 不得阻塞原 staging 的推进。
原 transfer 完成事实独立记录并提交 tick；最终 decode admission 是另一个结果，
不能把 success 藏在 gate 后导致 tick 与 transfer queue 循环等待。
失败保持失败处理，不降为普通 waiting。覆盖 READY/transfer 两种到达顺序。
普通 receiver cleanup 仍不释放 persistent mempool binding。

## D4：向 tick 提供真实 drain 事实

用户已确认：首版短暂停止整个 D TP 组的新 batch 提交，排空 overlap 工作，再完成释放。
其他未结束请求保留 KV、slot 和状态，排空并释放结束请求后恢复调度。
结果回调仅登记 pending release，并延迟原 cache/req_pool_idx 的回收。
安全边界必须覆盖 delayed sampling、result_queue 和未来仍可能引用旧 binding 的 host 工作。
device synchronize 只能证明已提交设备工作完成，不能单独证明不存在未来提交。

必须覆盖正常结束、abort、零次 decode 的 prebuilt 完成；fake warmup/capture dummy
不参与真实 acquire。用户接受该首版策略的停顿成本。flush 需保持结果只处理一次，
并在无未来 host 提交和设备访问后清理 binding，再通过 D1 统一释放。

使用独立 mempool pending release 列表；纳入 scheduler idle、资源检查与 sleep 判断，
不能复用原 deferred release 的超时仍释放语义。保留 health-check 专用 idle 语义，
控制消息待处理不等于有模型执行。一次 drain 尽量合并同轮已批准的释放请求，记录停顿耗时。

## D5：fault 的接入口和安全边界

普通请求取消走 CANCEL/drain/DONE；容量不足不是 fatal。
用户已确认：映射丢失、不可恢复协议错误、TP ownership 不一致直接报错终止，
不做同进程恢复。错误应包含 role/rank、request/attempt 和失败阶段等可用诊断信息。
进程仍能参与时，在 tick 汇总 fault 并阻止下一次模型调度。
进程死亡/卡死无法依靠成功 collective 传播，需要有界 communicator timeout/watchdog。

原有 PD heartbeat 不能覆盖 transfer Success 后的完整 mempool 生命周期。
timeout 或失联不能作为 P slot reuse/BM destroy 的依据。
本地 manager.close 的 drain 不证明远端 D 已停止读取；fatal teardown 必须有跨侧停机顺序，
无法确认时保持资源不可复用并要求协调停止，而非将故障伪装成正常 DONE。
报错不代表安全 drain，不伪造 DONE/ACK，不进入正常 slot 复用或未经确认的 BM 销毁。
本侧可协调的错误先汇总后报错；进程死亡/卡死依靠有界 timeout/watchdog，不能等待
一个必然成功的 collective。直接异常退出不能保证另一侧立即停机，跨侧故障感知及
人工协调两侧停止/重启的操作说明仍是接线工作，不能声称已实现远端保护。

## 代码依据

- P/D 四条循环：disaggregation/prefill.py、disaggregation/decode.py。
- 同步 intake：scheduler_components/request_receiver.py。
- 现有 poll reduction：disaggregation/utils.py，要求候选队列已经一致。
- ownership 变化：disaggregation/ascend/mempool_control.py。
- overlap delayed sampling：managers/scheduler.py。
- 正常和 prebuilt release：managers/scheduler_components/batch_result_processor.py。
- req_pool_idx 回收：mem_cache/common.py。
- handoff heartbeat 和失败标记：disaggregation/common/conn.py。

## 后续验证方向

Mac：16 个 control 实例模拟消息乱序/迟到、等待、cancel、同步 release 后复用，
断言每个成功 tick 边界同侧 free set/ownership 一致。模拟不能替代真实 collective 验证。
NPU：用户执行 normal/overlap、idle/paused、零 decode、abort、drain 后复用、故障停止验证。
