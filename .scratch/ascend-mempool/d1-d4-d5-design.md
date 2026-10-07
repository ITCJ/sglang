# TP control tick、drain 与 fault 接入设计

设计确认：2026-09-29；状态更新：2026-10-03。D1、D4 首版 drain 策略和 D5
直接报错终止策略已实现。用户已确认 02 真实服务 shadow readback 验收通过，
[ticket 02](issues/02-rank-pair-control-lifecycle.md) 已关闭。
本文记录 02③④的接线约定；代码路径、运行链路与验收证据见
[02 总结](ticket-02-summary.md)。正常请求、零 decode、Graph 读回和 slot 复用已有
本轮 NPU 证据；完整取消、故障注入矩阵仍由 06/07 验收，不能将本轮通过扩大到这些场景。

2026-10-07更新：用户在S6.3后授权将本地完整校验结果并入第一次TP同步，取消独立的
post-plan preflight同步。当前实现采用下文新流程；09-29/09-30的三轮设计保留作历史。
实现和本地验证见[两轮TP同步交付](ticket-03-prepare-summary.md)，新版本NPU复验待执行。

## D1：同侧统一推进

P16、D16 各自使用完整 TP CPU group；不建立跨 P/D 的32-rank collective。
固定 demo 验证 TP16 / DP1 / CP1 / PP1。P slot 与 D slot 独立选择。

在 `Scheduler.ingest_requests()` 处理输入后、返回前统一执行 tick，覆盖 P/D normal、
overlap 四条循环且位于 paused 判断前；不再给四条循环各加一个入口。
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

2026-10-07起的固定流程：

1. control以相关request和slot元数据准备每个候选，复用真实状态转换校验；不修改live
   ownership、不执行service副作用。返回结果及新增record需求，不复制无关历史。
2. 第一次all-gather一起同步逻辑观察、候选校验结果和record容量。原始proof/session
   仍留在本rank，本地验证结果绑定候选；未选中的消息错误不抢先改变其他请求。
3. 各rank根据相同输入形成计划。仅选全rank已验证的动作；同一room或P/D slot相互
   依赖的control动作分tick推进，slot来自本轮开始时的free set，record使用共同预算。
   新record admission最后执行，避免提前淘汰本轮其他动作仍需要的历史。
4. 提交计划，再做第二次all-gather汇总提交结果。全部成功后发送outbox；任何实际提交
   异常都进入既有fail-stop。接收线程仍只入队，新的完成事实留待后续tick。

完整校验和各rank一致准入的要求不变；proof正确本身不足以证明操作可执行。
空tick同样执行这两次collective。正常容量不足和资源冲突等待，沿用原deadline。

collective 数量/顺序不依赖本地消息数。空输入也参与。
提交失败不尝试继续服务；具体停机由 D5 负责。本流程不是跨进程故障下的原子事务。

所有 ownership 变化都由 tick 统一批准，包括 acquire、普通 safe rollback、
finish_drain、apply(DONE)，以及会消费 pending_done 的 finish_prefill_writes。
仅同步 acquire 不能维持相同 free set。

接线分工：`mempool_service.py` 收集真实请求/transfer/写入完成事实并管理待回收原生
资源；`mempool_tick.py::advance(...)` 封装完整协调流程。control 提供只读 snapshot，
保持协议 phase/slot ownership 的唯一来源；service 不维护第二套协议状态机。

正常 release 顺序：

- D：所有 rank 停止该请求的新提交并完成 drain → 清理旧 binding 引用 →
  同步提交 D slot release → 全部成功后发送各自 DONE。
- P：所有 rank 收到各自精确 binding 的 DONE，且全部 P writes 完成 →
  同步提交 P slot release → 全部成功后发送各自 RELEASE_ACK。
- D：收到全部对应 ACK 后关闭该逻辑 attempt；D slot 已可在 drain release 后复用。
- 同一 tick 可包含 release 和 acquire，出站旧 release 消息先于新 acquire 消息。
  acquire 候选取自 tick 开始时的 free-set snapshot，本 tick 刚释放的 slot 留待后续 tick。

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

### P request row 与 mempool slot 的独立回收（2026-10-01 已确认）

KV_READY 后原 main-KV transfer 仍可能经 staging 读取 P HBM，因此它不单独授权
native cleanup。正常 P 需原 handoff 成功、本地相关读写完成、无未来 host submission，
并消费该 row 的全部 completion 后，保存完成事实、detach 映射、回收原生 KV/pages
和 request row。新请求可复用该 row，但仍须独立 acquire 一个可用 mempool slot。
旧 P mempool slot 继续等待精确 DONE；slot release 顺序仍按 D1 执行。
取消/失败还须遵守原 transfer 的安全条件，不以本地 event 推断远端 writer 已停止。
这取代初版 runtime 中“P unbind 必须等整个 D drain”的过强约定；D 的 drain 要求不变。

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

原有 PD heartbeat 不能覆盖 transfer Success 后的完整 mempool 生命周期；02 已增加
mempool 独立 heartbeat、tick fault 汇总及 watchdog 接线。
timeout 或失联不能作为 P slot reuse/BM destroy 的依据。
本地 manager.close 的 drain 不证明远端 D 已停止读取；fatal teardown 必须有跨侧停机顺序，
无法确认时保持资源不可复用并要求协调停止，而非将故障伪装成正常 DONE。
报错不代表安全 drain，不伪造 DONE/ACK，不进入正常 slot 复用或未经确认的 BM 销毁。
本侧可协调的错误先汇总后报错；进程死亡/卡死依靠有界 timeout/watchdog，不能等待
一个必然成功的 collective。直接异常退出不能保证另一侧立即停机；跨侧故障检测与
人工协调两侧停止/重启的操作说明已随 02 交付，完整故障时序的 NPU 注入验证留给 07。

## 代码依据

- P/D 四条循环：disaggregation/prefill.py、disaggregation/decode.py。
- 同步 intake：scheduler_components/request_receiver.py。
- 现有 poll reduction：disaggregation/utils.py，要求候选队列已经一致。
- ownership 变化：disaggregation/ascend/mempool_control.py。
- overlap delayed sampling：managers/scheduler.py。
- 正常和 prebuilt release：managers/scheduler_components/batch_result_processor.py。
- req_pool_idx 回收：mem_cache/common.py。
- handoff heartbeat 和失败标记：disaggregation/common/conn.py。

## 验证状态与后续范围

CPU 契约测试已覆盖消息乱序/迟到、容量等待、cancel、同步 release 后复用、tick
一致性和部分失败处理；模拟不能替代实际进程故障下的 collective 验证。
02 的 NPU 验收已完成真实 TP16 服务的零 decode、普通请求、Graph 读回与 drain 后
slot 复用。具体计数及证据限制以 [02 总结](ticket-02-summary.md) 为准。
后续由 05/06/07 补齐多请求压力、idle/paused、abort、容量边界和故障停止矩阵。
