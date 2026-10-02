# 02: 16 对 rank 的正常控制闭环

**What to build:** 启动固定 P_i/D_i 的 16 个独立 pool，复用现有 PD bootstrap/ZMQ，
用真实 GLM-5.1 server 的 shadow 双写走通 acquire、binding、ready、drain 与释放。
每个请求在一侧的全部 ranks acquired 后才能推进，控制所有权持续到 release confirmation。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [01: 双机 mempool KV view 与 Graph 验证](01-mempool-kv-view-graph.md).

**Status:** ready-for-agent

**State:** open

## 当前执行入口（2026-10-02：继续demo，NUMA排查独立跟进）

小容量真实服务的真实top-k KV读回已实现；用户反馈三请求HTTP成功，日志检查通过
全rank数值、Graph及正常释放的前置条件，最后因检查器把request row与物理slot绑定而失败。
接下来用修正后的离线检查器重查现有日志，确认物理P/D slot复用，见最新Comments。
NUMA分配失败、大容量长尾和SDK失败清理的全部证据已汇总到
[09: NUMA分配跟进](09-numa-allocation-followup.md)；用户决定延期排查，09不阻塞本票
及03–08。以下历史Comments中的“下一轮NUMA实验”不再是当前执行要求。

具体推进顺序：

1. 使用`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`和已可运行的小容量配置，
   TP16、slots16、D graph batch width16。已有服务基线为context1024、P/D各512；
   本阶段不要求先复现或解决context16384的大容量启动长尾。
2. 在D实际selected top-k KV处接入独立BM读回buffer，复用已有view/UniDexCopy，
   从P prompt与D decode两处按prompt length及实际written range取数，对照旧路径的
   有效KV。旧路径继续供attention使用；读回比较须覆盖capture/replay后的真实数据，
   比较/报告放在完成事件之后，不在capture中做host同步或读取未完成的buffer。
3. 覆盖prompt/decode分界、D本地position0、slot映射、padding及zero-valid来源；
   top-k宽度2048下只比对valid entries。小context中无效列较多，不能声称覆盖了
   2048个有效token或8192容量的内容验证。错误报告包含rank/layer/请求及逻辑位置。
4. 同一轮真实服务测试完成零decode、实际decode、下一请求复用与全rank DONE/ACK，
   保存已有`verify_shadow_service.py`报告及新增数值比对结果。确认后完成02，进入03。

本票关闭仍以真实KV内容和正常生命周期证据为依据。03负责将读回路径变成attention
正式数据来源，并同时移除重复hostSHM/main-KV transfer；关闭hostSHM不是本票的临时
分配优化。小配置足以推进功能接线，较大容量及性能问题由09后续处理。

### 已完成实施安排与证据

本票此前分为两部分：part1–part3 已有代码的复核优化，以及part4服务接入。
第一部分接口优化已提交并推送为 `cfcafb4810`；用户反馈修改后的双机 writer gate 通过。
未勾选项仍待接线/验证，硬件证据边界见本票最新 Comments。
第一部分的勾选只表示本地实现与合同检查，不代表④的真实服务回收已经接通。
本节及两部分任务替代较早的接口/文件组织提案；旧③ S1–S6 安排移至 Comments
末尾保留历史。产品范围仍以父 spec 为准，D1/D4/D5 已确认的同步与安全要求不变。

| 已有部分 | 已取得的证据 | 当前边界 |
| --- | --- | --- |
| ① storage | layout、BM manager/view、writer、rank-pair startup helper 已提交；④已接入 | 小容量16对真实服务已有成功反馈；大容量/NUMA问题独立记入09，延期跟进 |
| ② control | 单pair状态机、TP tick与真实请求接线已提交；用户三请求日志检查走过全rank正常闭环与最终free=16检查 | 物理P/D slot复用待修正检查器重检确认 |
| ③ runtime/writer | 接口优化 `cfcafb4810`；双机writer gate两端各20条PASS；用户日志检查走过全rank真实readback与Graph条件 | 尚未收到完整日志和通过的result JSON |
| ④ service integration | 真实 selected KV readback实现已提交；Mac 146项CPU测试、23文件strict mypy通过；用户三请求HTTP成功 | 离线检查器误将request row轮换判为物理slot未复用，修正后待重检 |

保留 shadow 范围：P 原生 HBM cache、原 main-KV transfer、D staging/hostSHM、
Index K/其他 metadata 传输和 attention 消费路径继续工作；P/D 额外写入 mempool。
先验收无服务内 readback 的真实运行，再加入独立 top-k readback 完成本票。
原 main-KV transfer/hostSHM 的移除属于后续 cutover，不能在本票提前关闭。

本轮继续完成readback三请求日志验收，不再等待NUMA/大容量实验完成。

## 第一部分：part1–part3 已有代码的复核优化

### A1. 分离 request row attachment 与 persistent slot ownership

复核依据：`hardware_backend/npu/mempool/runtime.py` 初版按 `req_pool_idx` 保存 binding
和完成进度，文档要求 P unbind 等 remote drain；但原 P handoff 成功会回收原生 KV
和 request row。`req_pool_idx` 是请求索引行，不是 HBM KV page，也不是 mempool slot。
下文路径均相对 `python/sglang/srt/`，测试路径相对仓库根目录。

- [x] 将 runtime 的 row 清理约定改为明确的 `detach_row`：只移除设备表中的
  `row -> slot` 映射及本地 binding，不释放 control 持有的 slot ownership。
- [x] detach 前必须无 open forward、无该 row 尚未消费的 completion；调用方还须
  保证没有未来 host submission 会引用旧 row。保留固定设备表地址及安装事件顺序。
- [x] detach 返回不可变的本地完成事实（row/slot、prompt length、submitted/completed
  等实际所需字段）；返回 `KVWriteReceipt`，其中 `binding` 为本次 `bind()` 返回的
  `KVRowBinding` 对象。④ Ascend 接入层按已确认的 request attempt 保存，不再依赖旧 row
  查询进度。记录完成事实后再清映射/归还原生资源。
- [x] ④实际接线：正常 P native cleanup 同时要求原 handoff 成功、本地相关读写完成、无后续
  forward 使用该 row；完整 prompt 写入及 KV_READY 已作为独立事实记录。
  **仅收到/发布 KV_READY 不允许回收 P HBM cache**：此时原 KV 仍可能通过 staging
  路径传输。取消/失败走原 transfer 安全条件与取消/drain 流程，不能套用正常成功条件。
- [x] A阶段用 runtime/control 公共接口验证 row 可复用而旧 P slot 仍占用；旧 slot
  等精确 binding 的 DONE 和 P 写入安全条件满足才可释放。实际 native 回收、TP tick
  统一释放和 D whole-side drain 属于④接线，不能把此项当作服务验收通过。
- [x] 当前实现继续禁止带 pending completion 的 row detach。因此无需把 PD
  RequestIdentity/generation 引入 runtime completion，也不新增一套 runtime 协议状态机。
  未来若允许提前 detach，需另行设计 completion 身份，不能取消当前检查。

正常路径的两个回收时机：

```text
P prompt 写完并确认可读 -> KV_READY
原 handoff 成功 + 本地读写完成 + 无未来 row 使用
  -> 保存完成事实 -> detach row -> 原生 KV pages/request row 可复用
  -> P mempool slot 继续占用
D 停止该请求提交并 drain -> DONE
  -> P TP tick 核对 binding/本地写入安全 -> 释放 P slot -> RELEASE_ACK
```

### A2. 修正 Req 适配，收敛 runtime 的调用约定

- [x] 消除 `runtime.assert_bound()` 对 `req.req_pool_idx` 的错误假设；真实字段是
  `req.kv.req_pool_idx`。runtime 的校验接口接收明确的 row/写入期望等数据，不直接
  解析 SGLang Req、fake marker 或协议 phase。
- [x] 将真实 Req/fake 标识/attempt 与 row 对应关系的投影集中在第二部分的
  `mempool_service.py`。A阶段建立明确的 runtime 输入合同；B阶段补齐真实调用，
  不新增第三个 Req adapter 文件。不得只因 row 数字相同就认可新 request 的旧 binding。
- [x] A阶段以真实 `kv.req_pool_idx` 字段形状投影到 runtime，验证 missing/stale
  attachment 必须失败，包括完全相同 row/slot/prompt 的重用。fake 标识不进入 runtime。
- [x] B阶段验证 service 只有既有 fake 标识可跳过，真实请求必须持有批准的 binding；
  不得靠 invalid mask 静默接受接线错误。
- [x] 顺带检查现有公开方法：能在所属模块内完成的简单 offset/角色映射留在内部，
  不新增要求调用者记忆顺序的透传接口；必要的身份、容量、完成条件检查继续保留。
  每个新增/修改类和函数有简短介绍，以清晰参数表达必要约定。

### A3. writer 只保留显式 metadata 的一种接口

- [x] 删除仅由旧测试使用的 `MempoolWriteInputs` 与固定-input 兼容分支；统一为
  `write(values, *, slots, positions, valid)`，调用方不再在两套约定中选择。
- [x] 固定设备 binding 表由 runtime 管理；writer 保留 dtype/shape/extent/bounds
  校验以及全 invalid 仍 launch 的行为，支持 P eager 可变 chunk 行数。
- [x] 更新相关 CPU 测试、writer gate 调用和 README；保留有价值的 kernel/Graph
  行为回归，不为已删除的兼容接口保留包装层。

### A4. control 提供只读 snapshot，状态只由一个 owner 维护

- [x] 在 `disaggregation/ascend/mempool_control.py` 提供只读 request/binding snapshot
  和必要的 slot 可用状态；返回值不得暴露可修改的内部 record/dict 引用。
  当前接口为 `snapshot()` → `MempoolControlSnapshot`，每个 request 为
  `MempoolRequestSnapshot`；`owns_slot` 与保留的旧 slot coordinates 分开。
- [x] control 是 phase、P/D allocation、generation、协议 readiness 与 retirement
  的唯一来源。runtime 只掌握本地写入事实；service 只增加 Req 对应关系、deadline、
  待回收动作/完成事实等接线信息，不复制 `_RequestRecord` 状态机。
- [x] snapshot 读取无副作用；网络和完成回调只排队事实。`apply(DONE)`、safe rollback、
  `finish_drain()`、会消费 pending_done 的 `finish_prefill_writes()` 等 ownership
  变化都须由统一 tick 批准执行（④接线，当前以类文档明确该调用合同）。
  保留已确认的 session/proof/retired-generation 语义。

### 第一部分检查与交付

- [x] Mac 回归：pending completion 阻止 detach；消费完成后 row 可复用、完成事实仍可
  关联旧 attempt；control 仍拒绝复用尚未 DONE 的 P slot；旧 DONE 不影响新 owner。
  A阶段用可控调用方验证这些合同，B阶段再验证真实 scheduler/transfer 接线。
- [x] 覆盖真实 Req 字段形状、missing/stale binding、snapshot 不可修改且读取不改变
  phase/free set；保留可变行数、零有效行 launch、固定地址及 overlap counter 快照测试。
- [x] 运行 `ascend-mempool-test/` 适用 CPU suite、现有配置的严格 mypy、Ruff
  F/UP037/format、isort 与 `git diff --check`；记录本次实际结果，不沿用旧计数冒充重测。
- [x] 已交付接口/释放时序说明，用户授权提交推送并反馈修改后的双机 writer gate
  两端通过（10月1日日志，详见 Comments）。第二部分仍待后续实施。

## 第二部分：part4 服务接入实现

B节的勾选表示本轮代码与Mac检查完成，不代表真实TP、NPU Graph或模型精度验收。
真实shadow服务与readback的硬件项继续保持未勾选；本票仍为open。

### B1. 模块组织与已确认路径

保留当前8个非 `__init__.py` 的生产文件：NPU mempool 的 `config.py`、`layout.py`、
`manager.py`、`rows.py`、`offload.py`、`runtime.py`，以及 Ascend 的
`mempool_protocol.py`、`mempool_control.py`。不以文件数量为目标合并已有明确职责。
`rows.py` 继续保留与 `offload_v2` 的已知复制、来源及同步维护说明。

④只新增以下两个生产文件；有关 dataclass 放在所属文件内，不另建 types/events/actions：

| 新增路径（从 srt 开始） | 职责 |
| --- | --- |
| `srt/disaggregation/ascend/mempool_service.py` | `MempoolPDService`：唯一 SGLang 接入层，组装已存在的对象，对接真实 Req、原 transfer、准入、deadline、pending release、host drain/fault |
| `srt/disaggregation/ascend/mempool_tick.py` | `MempoolTPTick`：一次 `advance(...)` 完整封装 observations 汇总、统一 plan、preflight、commit、执行状态汇总及 outbox 放行 |

不新增 `npu/mempool/integration.py` 或 `ascend/mempool_queues.py`；不建立额外 queue
子类/通用 backend 框架。service 不逐步编排 tick 的内部 collective。
BM 初始化沿用 `MempoolKVManager.initialize_rank_pair()`，forward scope 放在 runtime；
接收/发送沿用 Ascend conn 的唯一 ZMQ reader 与既有发送入口。

已有 Ascend/NPU 路径的改动集中在上述8个文件、`srt/disaggregation/ascend/conn.py`、
`hardware_backend/npu/attention/ascend_backend.py` 和以下两个 NPU Graph 文件：

- `srt/hardware_backend/npu/graph_runner/npu_graph_runner.py`：真实 decode replay scope。
- `srt/hardware_backend/npu/graph_runner/npu_cudagraph_backend.py`：逐次 warmup/capture scope。

sparse manager/attention 仅核对当前调用链和必要的小范围适配；不成为 runtime owner，
本轮不抽取/改写原 `offload_v2` 行推导。

已确认的7个共享路径及其薄接口如下；所有协议/slot 决策实现放 Ascend/NPU：

| 共享路径（从 srt 开始） | 薄入口职责 |
| --- | --- |
| `srt/environ.py` | 注册 `SGLANG_NPU_ENABLE_MEMPOOL` |
| `srt/arg_groups/fields/disagg.py` | 声明容量和连接 server args；校验实现留在 NPU config |
| `srt/managers/scheduler.py` | 初始化 service、统一 tick、batch 准入检查，委托 abort/drain/pending-work/idle/fault |
| `srt/disaggregation/prefill.py` | finalize_bootstrap 副作用前 gate，handoff/失败时委托 native 资源回收 |
| `srt/disaggregation/decode.py` | preallocation 前 gate、原 transfer 事实和联合 readiness、失败回收 |
| `srt/managers/scheduler_components/batch_result_processor.py` | 正常结束、abort、零 decode 的统一回收委托 |
| `srt/model_executor/model_runner.py` | 实际 eager forward 外的薄执行 scope；行推导/计数/事件仍在 NPU |

`mem_cache/common.py` 和 `base_prefix_cache.py` 不在本次修改范围内；在原释放调用前
委托 service，安全后执行原回收动作。不得通过动态 monkeypatch、复制调度循环或改写
queue 内部 readiness 字段来减少共享文件数。

### B2. 配置、BM/runtime 与 control 的两阶段启动

- [x] `SGLANG_NPU_ENABLE_MEMPOOL=1` 同时要求 sparse KV offload；校验 NPU、Ascend
  PD、角色、TP16/DP1/CP1/PP1、BF16 MLA 及 peer 配置，拒绝 MLAPO/prefix reuse/draft
  等 demo 不支持组合。P 的 `PD_PREFILL_NATIVE` 也要启用 mempool。
- [x] `B_slots=16`，`S_P/S_D` 为 server args，默认16384；层数和 compact KV dim 取
  实际模型配置。启动 settings/校验集中在已有 `mempool/config.py`，不再建第二套配置。
- [x] P_i 启动 `base_port+i` store、D_i 连接，同 pool 局部 rank 为0/1；每个
  `AscendAttnBackend` 持有本进程 runtime。先完成 BM 映射/baseptr/固定设备表，再捕图；
  P 没有 sparse manager 时也必须正常初始化。核对共享 MF 与原 TransferEngine 生命周期。
- [x] 原 AscendKVManager/ZMQ 创建后 attach control/service，首个真实请求前完成
  role/rank、session、布局等握手和 BM peer 与 ZMQ peer 对应检查。mapping ready
  与 control ready 分开；不让 capture 等待尚未创建的 PD manager。
  对应关系校验需要实际读回 probe/nonce；仅写 probe 和交换 PROBED 不构成证明。

### B3. TP tick 与唯一状态来源

- [x] tick 在 `Scheduler.ingest_requests()` 处理输入后、返回前统一执行，覆盖 P/D
  normal/overlap 且在 paused 判断前；不在四条循环重复接线。未启用模式保持原行为。
- [x] 依次执行固定 snapshot all-gather、统一候选排序/plan、preflight、commit、
  执行状态汇总；全部成功后发送 outbox 并允许模型调度。空输入也参与，P/D 各用本侧
  TP CPU group，不建立跨32-rank collective，不按本地消息数改变 collective 顺序。
- [x] observations 保留到可统一处理；跨TP按逻辑 room/attempt 对齐，pair session/proof
  在本 pair 内验证。只读取 A4 snapshot，不访问/复制 control 的私有可变 record。
- [x] 所有 ownership 变化统一批准；安全 release 先于新 acquire，旧 release 出站消息
  先于新 acquire。普通容量不足等待；一致 preflight 后意外部分提交失败采用 fail-stop。
- [x] acquire 等待计入既有 PD bootstrap deadline，重试/换队列不重置；timeout/cancel
  由统一决策处理。tick 持续推进状态，不是固定间隔重置。

### B4. 准入、原 transfer 事实与联合 readiness

- [x] D 独立 acquire 后请求 P acquire；两侧各自16 ranks 一致后提交，P/D slot 可不同。
  安装/校验真实 row mapping 必须基于已批准的精确 attempt/binding。
- [x] P 在 `finalize_bootstrap()` 的 metadata 分配/sender init 之前只读 gate；未批准
  返回 False。关闭 optimistic prefill，检查包括测试强制 retry 在内的旁路不能绕过 gate。
- [x] D 在原 preallocation 循环的分配副作用前检查 approval；不靠返回 None 的
  `_pre_alloc()` 或过滤整个 `rids_to_check` 实现等待，以免漏掉失败处理/清理。
- [x] D 正常推进 `_poll_with_metadata_gate()` / `_poll_with_staging()`，在
  `pop_transferred()` 的汇合处统一记录原始 success/failure，再查询 tick 批准的
  mempool readiness。READY 与原 transfer 任意先后都可推进，failure 不降为 waiting。
- [x] sender/receiver 普通 cleanup 不释放 persistent control record/P slot；
  KV_READY 只证明 mempool prompt readiness，不证明原 KV/Index K/metadata 传输已结束。

### B5. forward scope、真实双写与 Graph 接入

- [x] 实际 eager、每次 warmup/capture、每次 replay 分别建立 runtime forward scope；
  scope 内完成期望行数校验、binding event 等待、begin/end 和完成记录。
  异常登记 fatal，不能执行正常 end 并伪造成功；具体协议处理仍在 service/tick。
- [x] NPU capture 的每次 run_once 直接调用模型，会绕过 ModelRunner；两次 warmup
  和一次 capture 分别包装，不能用一个 scope 包住全部导致重复层计数。
  replay 不执行 Python write_layer，但每次真实 replay 必须记账/记录完成事件。
- [x] P 保持 eager；D 固定 Graph metadata 地址、原地更新内容、同 stream 写入；
  保留逐层计数和历史快照。真实 batch、fake warmup、padding、空有效行明确区分，
  所有真实请求均核对 `req.kv.req_pool_idx` 与 attempt，不能全 invalid 静默通过。
- [x] P/D temporary compact KV 经既有 backend hook shadow 双写，原 cache/transfer/
  attention 继续运行；覆盖 chunk prefix、D 首次 local position=0、skip_topk 层及边界。
  完成 callback 只向 tick 提供事实，不直接发送 READY/DONE 或释放 slot。

### B6. native 回收、drain、idle 与 fault

- [x] 所有结束入口委托同一 service：记录 request attempt、原回收动作/`is_insert`
  及完成条件，每个动作只执行一次；保留原结果处理和输出。回调只登记，不递归 drain，
  不破坏 allocator free-group 的成对/非嵌套要求。
- [x] 正常 P 遵循 A1：原 handoff 完成且本地安全后 detach/free native resources，
  不等待 D 整个 decode 结束；persistent P slot 继续等待 DONE。取消/transfer failure
  还须满足原 transfer 的安全条件，本地 event 不证明远端 transfer writer 已停止。
- [x] D 统一暂停新 batch，排空 result_queue、delayed sampling、其他未来 host 提交
  和已提交设备访问；随后清理旧 row 引用、回收 native resources、统一释放 D slot，
  全部成功后发 DONE。正常结束、abort、零 decode 都覆盖；恢复未结束请求调度。
- [x] P 全 ranks 收到精确 DONE 且自身无未完成写入后统一 release，再发 RELEASE_ACK；
  D 可复用已释放 slot，但保留旧确认记录到 ACK。旧消息不能操作新 owner。
- [x] 独立 pending release 纳入 idle/资源检查/sleep，保留 health-check 特有语义；
  不使用原 deferred release 的超时强制 free。demo 需要 retract 时转明确 cancel，
  不进入假定同步释放完成的自动 retraction/rebootstrap 路径。
- [x] 接通基础 fatal：本侧可协调 fault 经固定 tick 汇总；死/卡 rank 用有界 timeout/
  watchdog。故障感知覆盖原 handoff cleanup 后的整个生命周期。报错/失联不等于 drain，
  不伪造 DONE/ACK，不自动复用未确认 slot/销毁 BM；交付双侧协调停止/重启说明。

### 第二部分检查与交付

- [x] Mac：从 service/tick 的完整入口验证16个 control 的乱序/迟到消息、空 tick、
  acquire/release 一致性；P row 已复用但旧 P slot 仍占用；READY/transfer 两种到达
  顺序；zero-decode、abort、overlap drain、恰好一次回收和不安全释放拒绝。
  复用第一部分和现有算法/协议回归，使用真实字段形状，不以浅层 wrapper 测试代替行为。
- [ ] 按 verification.md 与用户核对两部分实现，交付具体 launch 参数增量、P/D 顺序、
  请求脚本、日志关联字段、通过/失败判据。基于已跑通的 glm51dis.sh 样例，实际 IP/
  权重路径由用户环境决定；不把 Mac 检查标为 NPU/真实 collective 验收。
- [ ] NPU 由用户执行：16对启动、真实 GLM5.1 warmup/capture/replay、单请求 shadow、
  正常输出、完整生命周期与释放；先不读回 mempool。补测实际影响到的 writer/Graph 路径。
- [ ] 用户确认前一 gate 后，增加独立 UniDexCopy top-k（含2048规模）BM readback，
  对照原路径的有效 KV；读回结果不作为 attention 输入。两阶段通过才可关闭02。
- [x] 日志记录 role/rank、room/attempt、P/D slot/generation、native handoff、write
  completion、row detach、DONE/ACK 和实际回收；记录 tick/drain 耗时，优化留待 demo 后。

两部分的代码修改均先保持 unstaged，只有用户另行明确要求才 add/commit/push。

## Acceptance criteria

- [x] 第一部分 A1–A4 的本地接口优化及 Mac 回归完成；测试按真实
  kv.req_pool_idx 形状投影，runtime 不依赖 Req/fake/协议结构，writer 仅保留一种
  metadata 调用方式，control snapshot 只读且是协议状态的唯一来源。实际 Req/fake
  适配仍由④接入 service。
- [ ] P 的 KV_READY 不单独触发 native cache/request-row 回收；原 handoff 完成且
  本地相关操作/未来提交排空后可 detach 并复用 row，旧 P mempool slot 仍保持 acquired
  直到对应 DONE 和统一 release；实际日志/测试验证两个生命周期可以分离。
- [ ] ④按 service + tick 两个新增模块及已列明的7个共享路径薄接口完成；统一
  ingest_requests tick，Graph/eager scope 分别接到真实执行位置，没有额外协调/queue层。

- [ ] `SGLANG_NPU_ENABLE_MEMPOOL=1` 要求同时开启 sparse KV offload，并校验 NPU、
  Ascend PD backend、P/D role、TP=16、PP=1 与 peer 配置；无效组合清晰报错。
  P 的 `PD_PREFILL_NATIVE` 状态不会因 host-offload 属性为 false 而错误禁用 mempool。
- [ ] `B_slots=16`；`S_P` / `S_D` 可通过 server args 配置，默认均为 16384。
  每个 P_i 启动 store，D_i 连接 `base_port+i`；全部 ranks 的 BM
  映射与固定 Graph buffer 在 D capture 前就绪；协议、role/rank、session、
  dtype/layout、容量与 stride 的 peer 兼容握手在首个请求准入前完成。
- [ ] 在已有 PD receive path 处理 tagged mempool 消息，保持每个 PULL socket 一个 receiver。
  接收线程解析并排队，由顺序一致的 scheduler 协调执行 acquire、collective 和 release。
- [ ] D 先独立 acquire，再请求 P acquire；每一侧 16 ranks 对同一请求取得相同 slot，
  P/D slot 可不同。P 全 ranks acquired 且收到匹配的 `BOUND_ACK` 后才允许 prefill。
- [ ] 用 shared bootstrap room、pool session/epoch、request attempt 和 slot generation
  确认 binding；正常重复消息幂等，persistent ownership 不依赖 transient sender/receiver。
- [ ] 真实 GLM-5.1 P/D server 保留现有 sparse PD 路径，同时把 temporary KV 写入 P/D
  mempool，走通 `ACQUIRE`、`ACQUIRED`、`BOUND_ACK`、`KV_READY`、`DONE`、`RELEASE_ACK`。
  P mempool slot 的 ownership 持续到 D drain/DONE，普通 handoff cleanup 不释放它。
- [ ] 先由用户确认无 mempool 读取的 shadow 服务运行，再添加独立 UniDexCopy readback
  校验实际 top-k KV。校验数据不作为 attention 输入；有效内容正确且 decode 输出正常。
- [ ] D 的最后一次读取排空后释放 D slot 并发 `DONE`；P 确认该 attempt 的 D drain
  和自己的写入完成后释放 P slot 并回复 `RELEASE_ACK`。可从日志确认先后条件与最终可用 slot。
- [ ] mempool 与 MLAPO 同开启动报错；P/D runtime 均从 attention backend 接入，
  不依赖 sparse manager 生命周期。覆盖 skip_topk 层，避免 forward 漏写/重复写。
- [ ] 保留 D1 snapshot all-gather/preflight/commit，空输入仍同步；所有 ownership
  变化由 tick 批准，一致 preflight 后意外部分 acquire 失败报错终止。
- [ ] P 在 finalize_bootstrap 副作用前检查 tick-approved binding，未就绪返回 False；
  optimistic prefill 关闭，其他入口不能绕过；hook 不自行 acquire/release。
- [ ] D 在原 metadata/staging 两条 poll 路径均应用联合 readiness；原 staging
  继续推进，原 transfer 完成事实独立提交 tick，不与最终放行形成循环等待。
  覆盖 READY 先到/后到和原 transfer failure，失败不得隐藏为 waiting。
- [ ] D4 独立 pending release 管理纳入 idle/leak/sleep 判断；同轮释放合并排空，
  不沿用旧 deferred release 超时强制 free。涵盖 delayed sampling 和零 decode。
- [ ] 基础 fatal fault 明确报错退出，不伪造 DONE 或复用未确认安全的 slot；
  完整故障注入矩阵留给07，不能推迟正常服务依赖的 fault 接线。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

Mac 上通过消息/操作边界检查合法状态推进、身份匹配与正常重复消息。
NPU 上由用户启动全部 16 对 rank，验证启动兼容性检查、正常 binding、Graph 写入后 drain
和 release acknowledgement；日志应关联 bootstrap room、attempt、P/D slot 与 generation。
02 包含可安全运行的 waiting/timeout/cancel/fatal 基础接线；容量压力和部分 acquire
故障注入的系统验证归06，active cancel/peer fault 完整矩阵归07。

## Comments

### 2026-10-02：三请求通过，修正读回复用检查对request row的错误约束

用户在P机反馈 `verify_shadow_service.py requests --decode-tokens 32 --timeout 900`
三项均成功，completion_tokens分别为1、32、32；请求报告位于
`/tmp/mempool-02-readback-small/requests.json`。随后以 `/home/cryang/p.log`、
`/home/cryang/d.log` 执行 `check-logs --requests 3 --require-readback --readback-layers 78`
在 `check_readback_reuse` 报 `rank=0: no actual row/P/D slot reuse`。

按检查器执行顺序，此异常说明前面的全16rank mapping、Graph capture/replay、逐请求
生命周期和最终free=16、零decode与真实逐层KV数值/来源/分界覆盖检查均已走过。
这是依据用户提供的控制台栈得出的部分验收证据；未读取远端完整日志或JSON，不能据此
宣布物理slot复用已验证，用户尚未反馈本轮代码版本与生成文本检查结果。

根因：离线检查器错误地要求不同attempt同时复用 `(row, prompt_slot, decode_slot)`。
实际D使用 `DecodeReqToTokenPool`：从空闲列表头取row，释放时追加到队尾，因此即使
P/D物理slot已复用，request row仍会正常轮换。`CONTEXT.md`定义request row与物理slot
独立。检查器还错误地用BM的16slot上限约束row；D预分配的请求表可大于16行。

修正仅影响离线验收：各rank仍须有不同decode attempts实际复用同一P/D物理slot组合；
row保留正整数校验，报告独立输出 `rows` 和 `row_reused`。不改变服务分配策略。
若 `row_reused=false`，本轮不声明取得真实服务row复用的硬件证据；全feature的独立
request-row复用覆盖要求仍保留。数值、Graph、ACK及物理slot复用要求均继续检查。

Mac先添加FIFO row轮换、P/D存储复用的完整日志回归，复现用户同一异常（红），修复后
6项日志检查测试通过（绿）。覆盖row 2→3和16→17，并保留物理slot不复用、非法位置
及相同attempt的拒绝。完整CPU suite 147项通过，修改的检查器strict mypy通过，
两份Python文件Ruff、format、isort通过；启动脚本 `bash -n` 与两仓库
`git diff --check` 通过。本机未安装pre-commit，未运行仓库全量hooks。
规范与spec两条独立复核均无finding。启动脚本的验收注释同步为
`ascend-sglang-script/main` 提交 `5e35b2f`。

更新检查脚本后，对原日志重新运行原 `check-logs` 命令即可，无需重启NPU服务或重发
请求。若新版仍报 `no actual P/D slot reuse`，才按读回说明在全部ACK后补请求。
**等待用户重检NPU日志并确认验收**；本票保持open，不推进03。

### 2026-10-02：同步glm51mempool启动脚本并精简诊断输出

用户要求更新 `ascend-sglang-script/pd-disaggregation/glm51mempool.sh` 并推送两仓库。
脚本P/D实际参数从context16384、各8192恢复到本轮context1024、各512，TP16与D Graph
width16保持一致；开启 `SGLANG_NPU_MEMPOOL_READBACK=1` 和偶数NUMA列表。
设置 `SGLANG_NPU_MEMPOOL_DIAGNOSTICS=0`，关闭周期WAIT/内存/栈快照及主动提升MF INFO；
保留默认INFO的readback结果、Graph、DONE/ACK，供完整日志验收。
两侧可通过 `LOCAL_HOST1` 指定本机地址，日志统一在 `/tmp/mempool-02-readback-small`，
`LOG_DIR`可指定新目录。脚本尾部已更新三请求和 `--require-readback` 检查命令。
原TransferEngine的 `ASCEND_MF_STORE_URL` 改为从 `P_IP[0]` 派生，修改P地址时无需另改。
脚本提交为 `ascend-sglang-script/main` 的 `8074c0c`。

[读回说明](../../../ascend-mempool-test/READBACK_SERVICE.md)同步增加直接启动脚本、router
命令、日志关键字及数值字段解释，区分HTTP成功、单rank数值成功和全rank验收通过。
Mac已通过 `bash -n`、两仓库 `git diff --check`，并用CLI帮助核对交付参数。
规范审查无finding；spec审查指出的独立store地址遗漏已修复并定向复核通过。
本次仅改脚本与交付文档；真实读回实现为 `c4ec7c6b67`，其146项CPU测试已在前轮通过。
仍等待用户执行小容量NPU验收，本票保持open。

### 2026-10-02：02真实selected KV读回实现，准备小容量验收

用户要求先保存基线，再完成本票真实KV读回并以小容量测试。原运行代码已提交；
本轮开始时的未提交文档整理为基线 `7eed14f9d0`
（`docs(npu): baseline mempool readback plan and defer NUMA investigation`）。

实现路径：

- `SGLANG_NPU_MEMPOOL_READBACK=1` 默认关闭，仅D执行。将01的`SparseKVCopy`
  提升到生产mempool目录供gate和服务共用；新增`readback.py`集中设备比较与完成证据。
  这是第二个gate新增的存储代码，不新增协议、queue或第三层Req adapter。
- 旧路径hit/miss事件已等待后，从P/D独立slot读取实际top-k位置，与旧selected KV
  逐元素比较。attention仍使用旧buffer；本轮不关闭hostSHM或原main-KV transfer。
- service从批准的control snapshot传P slot；runtime固定表保存P/D slot、prompt
  length及本轮实际提交的decode写入范围。同stream先写本层KV再读回，包括D position0。
  负索引/padding排除；正索引超过已写范围会失败，不能当成无效列吞掉。
- 各层共用同shape的scratch。forward末尾clone小型逐层结果并记录事件，事件完成后
  才读host快照、核对层/请求覆盖并累计汇总；overlap和后续replay不能覆盖前一轮证据。
  差异进入fault，错误给出rank/layer/room/rid/attempt/position/feature及P/D slot。
- D drain覆盖新增读取。正常detach前输出逐请求`readback_result`；零decode只报告
  `zero_decode`，不声称存在KV比对。P仍按原DONE/ACK协议释放slot。
- `verify_shadow_service.py check-logs --require-readback --readback-layers 78`
  同时检查全rank生命周期、真实readback replay、完整逐层次数、两来源/分界内容证据。
  每个rank必须有不同decode attempts实际复用同一组row/P/D slot，不能把两个串行
  HTTP响应当成复用证据。任一缺失不通过，数值及复用报告随JSON保存；旧生命周期
  模式保留但不能替代本轮验收。

Mac实际执行：新增用例按runtime/device事件、SDK copy及日志边界红→绿；完整CPU suite
146项通过，23个源文件strict mypy通过，Ruff检查、格式检查及isort通过。
回归包含不同P/D slot、D首行、错误逻辑位置、内容差异、pending event、overlap快照、
16×2048 padding、zero-valid捕图调用、row复用、service故障保留、缺rank/layer报告，
以及验收日志未真实复用/位置缺失或越界/重复attempt的拒绝。

`code-review`已按规范与spec两条独立审查完成：修复新增数据容器未用msgspec.Struct、
交付文档地址未使用占位符，以及日志gate未核对真实复用三项。两位审查者定向复核
确认问题均已解决，无剩余finding；最后一次146项完整CPU suite和23文件strict mypy通过。

交付命令：[READBACK_SERVICE.md](../../../ascend-mempool-test/READBACK_SERVICE.md)。
context1024、P/D各512、偶数NUMA、TP16/slots16、D Graph width16；零decode、decode、
复用三个请求，回传P/D完整日志、requests/result JSON及代码版本。
**等待用户执行NPU验收**；本地检查不代表BM真实数值、NPU Graph或模型精度通过。
top-k2048宽度只验证valid entries，不声称2048个有效token或8192容量已覆盖。
本票保持open；09仍延期，本轮验收确认后再进入03。

### 2026-10-02：用户决定将NUMA排查移出demo主线

用户认为可以继续推进；最终设计会关闭旧hostSHM，因此不在shadow阶段继续耗费时间
优化BM分配。已新建09，汇总环境/版本、NUMA策略演变、已完成对照、最小复现、退出
异常、大容量长尾、日志路径、未证实假设及未来验收；保留本票历史原文供追溯。
09延期且不增加blocking edge。当前工作转向本票剩余top-k readback，随后03正式读取
切换和移除重复存储、04短请求Graph/模型验收。三请求回归和读回仍待完成，未据此关闭02。
本轮仅整理ticket/执行顺序，未修改运行代码，未执行新NPU测试。

### 2026-10-02 20:42–20:45：context 16384、P/D各8192时D侧大页分配出现长尾

用户回传D侧服务日志片段，`max_context_len=16384`，P/D布局均为78层、16 slots、
8192 tokens、1 head、576维、bfloat16。服务提交号及完整日志路径未提供；日志确认
`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`生效，所示TP按列表轮转，
HAL的numa与选定节点一致。

- 每rank逻辑KV为10.96875GiB，加probe并按1GiB对齐后本地BM为11GiB；
  16rank单机合计176GiB，0/2/4/6各4池、44GiB。22GiB的GVA保留范围涵盖
  P/D两方贡献，不代表每个worker都在本机分配22GiB物理内存。
- 日志中单worker原有hostSHM为25,033,522,176 bytes（约23.31GiB）；
  若16worker均采用此配置，原hostSHM合计约373.03GiB，与BM合计约549.03GiB，
  尚不含权重及其他内存。此前双机偶数NUMA gate仅为1GiB/池、4GiB/节点。
- TP13/14/12/11的create2约1.4–1.7秒完成。部分rank的1GiB页尝试返回6，
  SDK保持所选NUMA并改用2MiB页后成功：TP1为21.302秒、TP5为79.821秒、
  TP2为153.213秒、TP9为169.444秒。这些rank随后join、映射检查、runtime
  初始化均完成，并进入decode Graph capture。
- WAIT快照中TP1/2出现`alloc_contig_range`、`lru_add_drain_all`或
  `__drain_all_pages`；TP5/6在`devmm_master_alloc_numa_large_pages`，
  TP9/10在giant-page分配调用中的`devmm_master_free_giant_pages` /
  `devmm_master_free_one_page_by_size`。等待定位于驱动本地物理页分配/释放；
  片段不足以区分碎片化、并发争用及驱动行为各自的影响。
- 快照仍有大量空闲内存、可见memory.failcnt=0，未出现总内存或cgroup OOM证据。
  `HugePages_Total=0`也出现在已成功rank的快照中，不能单凭此值判定分配失败原因。
- 截至片段末尾，TP6/10（均node4）已各等待约165秒；二者约150秒的1GiB页
  尝试返回6后正在2MiB页路径。尚无二者最终END/FAIL；其他遗漏rank的完成状态
  也不能从此片段补全。`[MEMPOOL_INIT] READY`仅表示该worker的BM/runtime就绪，
  未证明本轮全部16rank、Graph capture、PD握手或服务请求完成。

本轮agent在Mac核对现有layout/runtime及本地MF分配源码，计算容量并记录用户硬件
反馈；仅修改本记录，未修改运行代码、未执行新NPU测试。继续等待本轮剩余rank、
Graph及服务完成日志；ticket保持open。

### 2026-10-02：实际服务改用显式NUMA节点列表，无效配置提示并整体回退

用户要求取消COUNT变量，以`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`
让实际服务按TP rank平均分到所选节点；配置不存在的节点时提示并恢复默认分配。
此配置取代下方历史记录中的COUNT用法，不把旧版本的NPU结果记为新版本验收。

- `mempool/manager.py`的共用创建入口按`nodes[tp_rank % len(nodes)]`选择节点，
  保留列表顺序，使用真实TP rank；16rank选择0/2/4/6时各4池。
- 在BM分配前读取本机`/sys/devices/system/node/online`校验整份列表。任一节点
  不存在或未在线时，WARNING列出无效ID与本机在线节点，整份配置回退`flags=0`。
  空值、重复ID、非法格式、超出MF可显式编码的0..126或拓扑无法读取/解析，也提示后回退。
  没有配置时直接使用flags=0。显式绑定不会在HAL失败后尝试再次create2。
- 删除COUNT的环境变量注册和读取；ENV诊断及创建日志改记新变量和`local_numa_nodes`。
  `run_bm_startup_gate.sh --even-numa`也改为export节点列表，checker要求该列表及
  每卡HAL节点与预期一致，默认分配不能满足偶数节点gate。
- 池容量、BM句柄ownership及双侧drain/close协议沿用已有实现；新增检查只决定
  本地DRAM分配flags。修改列表后须重启服务，已有pool不会迁移。

Mac实际验证：先运行新增的不存在节点告警用例，旧实现因无告警失败；实现后
`test_pair_startup test_bm_startup_gate test_pool test_startup_diagnostics`共36项通过。
覆盖16rank/P/D映射、非顺序/不连续/单节点列表、无效列表整体回退、拓扑读取失败、
旧COUNT无效、默认分配未访问拓扑，以及gate拒绝默认或错误节点列表。
Ruff F/I/UP037、format、mempool及checker的严格mypy（9文件，ignore-missing-imports）
通过；launcher的bash语法及三份运行文档的bash代码块语法通过（IP/NIC占位符先替换），
git diff --check通过。本机无torch/NPU环境，未运行真实BM分配或模型服务。

等待用户执行NPU验收：P/D同步本次代码后，在各自启动服务的shell中export新变量
`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6`，先P后D，沿用context1024、
P/D capacity512，核对全部16rank的NUMA节点、BM/runtime ready、D Graph及服务ready。
需要独立确认新配置入口时，仍使用[双机16池命令](../../../ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md)，
要求两侧全部worker退出0且两项startup/even-NUMA汇总通过。回传代码版本和两侧日志；
最新双机通过记录仍是下方旧COUNT版本，本票保持open，真实KV及其余生命周期验收继续待测。

### 2026-10-02：用户确认P/D偶数NUMA、各16个1GiB池全部通过

用户回传`run_bm_startup_gate.sh --even-numa`两侧完整终端汇总，并确认“感觉没有问题”。
对应交付版本为`5b100c0001`；本次未另外回传服务器git rev-parse或原始逐卡JSON。

- P：`npu1-31` / `10.120.72.31`，rank0，目录`/tmp/bm-even-numa-p/run.aAPaAl`。
- D：`npu1-32` / `10.120.72.32`，rank1，目录`/tmp/bm-even-numa-d/run.EreEQi`。
- 两侧device0–15全部PASSED，均输出`ALL_EVEN_NUMA_CHECKS_PASSED`和
  `ALL_BM_STARTUP_CHECKS_PASSED`，NUMA统计均为`{0:4,2:4,4:4,6:4}`。
- 每侧16个1GiB贡献同时存活，每个偶数节点请求4GiB；双侧共32个worker均正常退出。
  按checker通过条件，HAL成功分配节点、映射后64字节peer probe、本机ready barrier、
  双侧drain/close及exit0检查通过。本轮未重现create2失败或退出134。

- [x] 本次临时规避的独立双机16池验收：每侧NUMA0/2/4/6各4个1GiB池通过。
- [ ] 模型加载、原D hostSHM等实际服务上下文中重测偶数NUMA策略。
- [ ] 本票其余真实服务生命周期与KV readback验收。

结论限于当前独立1GiB/rank配置：偶数节点规避已获双机实测支持；不代表奇数节点
HAL6或SDK失败析构问题已修复，也不替代Graph/真实KV验收。无需重复相同独立gate。
下一步两侧在启动原服务前显式export COUNT=8（测试runner的export不回传父shell），
先P后D，保持context1024、P/D capacity512和每rank1GiB。核对全部16rank的NUMA选择、
BM/runtime READY、D Graph capture和最终服务ready，再继续真实请求验证。
用户提供的是终端汇总；原始逐卡日志、exits.tsv和summary保留在上述服务器目录。
本轮仅记录实际反馈并核对本地服务启动配置，未执行新NPU测试；本票保持open。

### 2026-10-02：临时跳过奇数NUMA，并交付P/D各16池的小容量测试

用户要求先避免奇数NUMA节点，再做P/D各16个mempool平均分布于偶数节点的测试。
显式COUNT配置现改为`2 * (tp_rank % ((N + 1) // 2))`；N仍为全部本地节点数。
N=8时TP0/4/8/12→0，TP1/5/9/13→2，TP2/6/10/14→4，TP3/7/11/15→6，
P/D按相同规则独立选择。未配置仍为flags=0。此临时策略取代早先`tp_rank % N`的
显式绑定行为，用于规避已观察到的奇数节点HAL6，不宣称修复驱动或SDK失败清理。

扩展已有`run_bm_startup_gate.sh --even-numa`，复用原create/join、peer probe、
本机ready barrier及双侧drain/close。该模式固定device0–15、COUNT=8、诊断开启，
78层/16slots/512tokens/BF16 dim576，对齐后每池1GiB、本机16GiB、每个偶数节点4GiB。
每个worker到齐前保持池存活；均到齐后验证双侧完成并退出。不加载模型、不执行Graph。
不带新选项的11GiB/rank模式保留。两侧命令与端口、前置条件、结果边界已补入
`ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md`，并更新两个README和环境变量注释。

runner新增`exits.tsv`记录真实子进程退出码。新`check_bm_even_numa.py`逐卡核对
退出0、1GiB贡献、64字节peer probe、16池同时ready、实际create flags和HAL成功日志。
要求0/2/4/6各4个成功池，生成`even-numa-summary.json`；缺失worker、错节点、
缺少HAL证据、错误容量或成功报告后SIGABRT/134均不能通过。同节点页大小fallback允许。
HAL日志确认调用时的NUMA选择，物理页落点仍需结合NUMA内存观测。

Mac实际检查：

- 先将SDK边界NUMA回归改成偶数节点期望，原实现失败；修改manager后通过。
- `PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/tests/unit python3 -m unittest
  test_pair_startup test_bm_startup_gate test_pool test_startup_diagnostics -q`：33项通过。
  覆盖P/D16rank分配、奇偶节点数边界、两侧16worker命令和1GiB布局、报告校验及退出134。
- 严格mypy检查NPU mempool包及新checker：9个源文件通过；本机没有torch，使用
  `--ignore-missing-imports`，不代表torch/NPU运行时通过。未重跑依赖torch的完整CPU suite。
- Ruff F/I/UP037（first-party与既有isort配置一致）、5个改动Python文件format、
  runner与5个文档bash代码块的`bash -n`、布局`--describe`、checker`--help`通过。

等待用户执行NPU验收：P/D两侧均须退出0、显示`ALL_EVEN_NUMA_CHECKS_PASSED`且
counts为`{0:4,2:4,4:4,6:4}`，回传本轮summary、退出码表及异常卡完整日志。
此次只交付本地实现和可运行测试，未执行服务器双机测试；真实服务及KV readback仍待验收。
本票保持open。

### 2026-10-02：失败日志尾部确认 RESULT 后 HYBM 析构异常和堆损坏

用户补齐 `/tmp/bm-numa.qDNtCX/device-{0,1}-node-1.log` 尾部。两组次序相同：
HAL在node1分配返回6 → create2抛出RuntimeError → store/BM uninitialize结束 →
Python打印RESULT → 约1–2秒后HYBM继续析构预留地址 → glibc abort。
最后的 `timeout: the monitored command dumped core` 是子进程崩溃报告；
本次并未耗尽90秒时限。

原生退出阶段的直接证据：

1. 对同一 `0x280040000000`，首次 `FreeReserveLva` 找到记录后，
   `HalMemAddressFree` 返回-8。
2. 后续同地址释放再次进入，记录已不存在；segment仍保留reserved VA状态，
   最后 `~HybmVmmBasedSegment` 报 `Destructor cleanup failed, ret:-6`。
3. glibc报 `malloc_consolidate(): invalid chunk size`，进程SIGABRT/134。
   这证实原生堆状态损坏被检测到；仅日志不能确定最早的越界写/UAF/double-free位置，
   也不能把反复清理日志直接当作重复成功释放同一物理块的证据。

本地MF参考代码 `hybm_def.h` 定义 `BM_UNDER_API_UNLOAD=-8`，
`DlHalApi::HalMemAddressFree` 在函数指针为空时直接返回-8；`hybm_uninit()`
调用 `DlApi::CleanupLibrary()` 清空指针。该路径与BM uninit之后继续析构的日志吻合，
强烈支持失败对象未在底层API卸载前清理完毕。`UnReserveMemorySpace`在调用HAL前
先移除VA管理记录，HAL失败后保留segment状态，解释后续“record not found”提示。

还找到本地已有提交 `2e47225066f0372c62e524008f9c79f41ea1ac8c`
(`[core] fix: rollback bm init state when failed`)：初始化失败时先设置inited_
以确保UnInitalize执行，并在create2失败分支移除manager中的entry。这是应与服务器
构建核对的相关修复。当前部署commit `c01f3ad842b9ff7412681a44b67141ce7a124c6d`
不在本地Git对象库，无法证明其缺失此修复，也未验证该提交能修复本例堆损坏。

首发node1分配失败与后续退出异常均有证据。修复退出清理后，非法/不可分配请求应
被报告并以正常非零码结束，仍需独立解决HAL6。原探针保持不变，可用于对比SDK构建；
如需定位abort调用栈，用GDB运行device0/node1最小case并在SIGABRT处取所有线程栈。
节点2..7与D侧的能力验证仍待执行。本轮只补充记录/说明，diff检查通过，未改SDK或
运行脚本，未提交/推送，未执行新的NPU测试。本票保持open。

### 2026-10-02 15:24–15:26：P 独立交叉测试确认失败随请求 node1 变化

用户在P容器 `npu1-31` / `10.120.72.31` 执行已推送的独立探针入口
`ascend-mempool-test/scripts/probe_bm_numa.py`，world_size=1、每case独立进程串行、
1GiB HOST/SDMA，日志目录 `/tmp/bm-numa.qDNtCX`。脚本已随 `0c49f55296`
推送；未单独核实服务器HEAD。用户回传全部六组退出码与HAL/RESULT摘要。

| device | 默认（flags0） | node0（flags128） | node1（flags129） |
| --- | --- | --- | --- |
| 0 | HAL0，allocation_ok=true，exit0 | HAL0，allocation_ok=true，exit0 | HAL6两次，create2失败，exit134 |
| 1 | HAL0，allocation_ok=true，exit0 | HAL0，allocation_ok=true，exit0 | HAL6两次，create2失败，exit134 |

node1两次HAL调用：device0为44/18微秒，device1为49/17微秒，均为1GiB页失败后
2MiB页重试失败；成功分配约101–126毫秒。脚本总耗时约9秒含导入和SDK初始化/清理，
不能误认为HAL本身耗时9秒。默认路径numa:4294967295表示传入-1，未证明物理落点。

结论：在测试过的device0/1与node0/1组合中，失败随指定node1变化；奇数device1
在node0能成功。服务模型加载、TP16并发、跨机join和Graph不是复现该故障的必要条件。
结合14:59两侧奇数节点充足MemFree仍失败，优先检查指定节点上的HAL/P2P DDR分配
条件或驱动问题。没有证据宣布全部奇数NUMA节点永久不支持，也不能从P测试替代D验收。

失败两组均已输出 `failed_stage=bm.create2`、`allocation_ok=false`、
`cleanup_errors=[]` 的RESULT，然后shell报告Aborted/134。134对应SIGABRT；
捕获的Python RuntimeError按脚本应返回1。因此还存在RESULT之后的原生退出异常，
尚未定位库/线程/析构点，cleanup_errors仅表示显式Python清理没有捕获到异常。
下一步保留两份失败日志最后80行；不能用强制os._exit绕过退出来掩盖这个证据。

现有探针已可用于固定device1测试node2..7，补齐候选节点，并在D独立验证。
NUMA_COUNT=4只会轮转0/1/2/3；若实测可用集合为0/2/4/6，需要显式列表配置，
不能将现有count变量静默解释为偶数节点数。恢复既有默认路径可移除COUNT变量，
但默认路径成功不等于指定节点能力或大容量验收通过。

本轮仅记录用户NPU结果并补充诊断说明，未改运行代码，未执行新的NPU测试或推送。
公开驱动头文件确认NUMA参数含义，未查到可证明本部署奇数节点限制的依据；
具体驱动分配约束仍须原生日志/当前版本实现确认。本票保持open。

### 2026-10-02 14:59：清缓存后仍按奇偶分化，准备独立 device/NUMA 交叉诊断

用户清理干净页缓存后再次启动相同1GiB/rank、TP16、NUMA_COUNT=8配置。
本次失败时的快照中，P/D各NUMA节点仍有约160–200GiB空闲内存，
Mems_allowed_list=0-7，可见memory cgroup failcnt=0、limit近似无限。
因此撤回“清掉缓存即可恢复”的预期；普通节点空闲容量不足无法解释本轮失败。
这不等于证明驱动所需页类型、P2P可分配区域或其他限制均已满足。

- P/D都出现奇数TP→奇数NUMA节点的本地HAL分配失败；可见D TP1/3/5/7/9
  在对应node1/3/5/7的1GiB页和2MiB页尝试均返回6，TP11/13/15也有create2失败栈。
  HAL失败调用通常仅几十微秒；create2整体约0.2秒包括其他SDK初始化。
- 偶数rank成功分配、跨机import/mmap，并到达mapping/runtime ready；D若干rank
  随后开始Graph capture。其他rank失败触发SIGQUIT关闭服务，不能据此确认Graph完成。
- D本次已连接P store并亲自执行create2，不能沿用14:08那轮“D只因P退出等不到store”
  的解释。部分初始MAPPING_PENDING后约1秒变为ready，是异步join期间的暂态。
- HugePages_Total/Free=0、缺libhcom和extend library提示也出现在成功rank，
  这些信息不能单独解释失败。普通MemFree和HAL可分配P2P DDR不是同一指标。
- 当前device_id=tp_rank，numa_node=tp_rank%8，device与node奇偶性完全重合。
  尚不能断言奇数NUMA永久不支持或奇数device故障。新一轮P的TASK_QUEUE_ENABLE=1、
  MULTI_STREAM未设置，D为0/1，也应在后续服务对照中记录环境差异。

已核对本地MF参考源码：显式节点经flags进入MEM_HOST_NUMA_SIDE的prop.devid；
host申请使用MEM_P2P_DDR_TYPE，仅在同节点从giant降为huge页重试。
公开CANN驱动头文件也明确该side下devid是NUMA ID，但未确认当前部署驱动的节点资格限制。
参考链接：https://gitcode.com/cann/driver/blob/master/pkg_inc/ascend_hal_base.h

新增独立诊断脚本 `ascend-mempool-test/scripts/probe_bm_numa.py` 和运行说明
`ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md`：串行world_size=1、每case独立进程，
固定1GiB HOST/SDMA，分别组合device0/1与默认/node0/node1。直接传独立节点flags，
无需模型/对端/Graph；保留失败stage、allocation结果与cleanup错误。
用于区分节点约束、device上下文、只在模型/并发条件下出现的失败；不改变服务绑定语义。

Mac实际验证：6个describe case的device/node/flags/容量、3个无效输入拒绝、help、
Python语法及说明中bash命令语法通过；Ruff F/I/UP037通过，已按formatter格式化。
未执行真实HAL分配，NPU上的复现与结论仍待用户运行。未提交或推送，本票保持open。

### 2026-10-02 14:08：显式 NUMA 选择生效，P 部分节点的 HAL 分配失败

用户提供启用 `SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT=8` 后的 P/D 日志。
本地已推送 NUMA 改动 `c89416cd4c`，未独立读取服务器 HEAD；服务器 MF 报告
1.1.4 / `c01f3ad842b9ff7412681a44b67141ce7a124c6d`，驱动
`V100R001C10SPC009B220`。本次仍是 TP16、16 slots、78层、S_P/S_D=512，
实际 BM 本地贡献 1GiB/rank，即每机16GiB、按8节点轮转计划每节点2GiB。

可直接确认的执行事实：

- P TP15→node7/flags135、TP9→node1/flags129、TP5→node5/flags133、
  TP11→node3/flags131；HAL 日志中的 `numa` 与请求节点一致。
  这些 rank 的 1GiB 分配先尝试1GiB页，返回6后改用2MiB页，仍在同一节点返回6，
  随后 `bm.create2` 失败。后续 TP1/3/7/13 的栈也停在 create2。
- P TP8→node0、TP4→node4 的 HAL 返回0；TP10→node2 有 create2 END。
  已贴片段没有所有成功 rank 的完整 HAL 结果，不能据此宣称全部偶数节点都验收通过。
- 本地 MF 参考源码把6定义为 `HAL_OUT_OF_MEMORY_ERROR`；host 分配路径仅在同一
  指定节点上从 giant page 降到 huge page，未实现跨节点重试。服务器日志也显示
  同节点两次失败。参考源码与服务器 MF commit 不完全相同，不能扩大推断其他驱动行为。
- P 约14:08:51–52在失败清理后抛出 scheduler 异常，父进程收到子进程 SIGQUIT 并
  调用 `kill_process_tree`。日志末尾的 Killed 有应用退出链路证据，不能直接定性为
  Linux/cgroup OOM killer。D 约14:08:53才开始等 P store，之后 Connection refused；
  此次所贴 D 日志尚未进入 BM create2，未验证 D 的指定节点分配。
- 成功创建的 P rank 当时报告 `MAPPING_PENDING missing_rank=1`，与 D 尚未加入
  相符；这些等待日志不构成独立的 KV layout、Graph 或控制协议故障证据。

P 失败附近快照的 MemFree（GiB，多个 rank 并发采样，非隔离的前后差值）：

| NUMA node | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| MemFree | 67.24 | 0.92 | 65.21 | 0.25 | 35.08 | 0.15 | 24.95 | 0.25 |

所见失败节点的 MemFree 均小于单次1GiB申请，但全机 MemAvailable 仍约1.76TiB。
这支持优先排查指定节点上的驱动可分配内存，尚不能区分局部内存压力、可回收缓存、
大页碎片或驱动分配限制，更不能宣称奇数节点永久不可用。Mems_allowed_list=0-7；
可见 cgroup limit 近似无限、failcnt=0。HugePages_Free=0 同时出现在成功分配的节点，
不能单独用它解释失败。

下一轮可先在 P 采集完整 `/sys/devices/system/node/node*/meminfo` 与
`/proc/buddyinfo`，补齐现有快照缺少的缓存/可回收内存和空闲块分布；必要时对照驱动
分配日志。保持服务容量不变、移除该 NUMA 变量可对照此前默认分配模式；需先结束
本轮仍在等待的 D 进程。是否恢复启动以两侧全部 rank 的 mapping/runtime ready 为准，
成功不等于已确认物理落点或大容量可用。节点数设为4只会使用0/1/2/3，不能表达
0/2/4/6；若需后者，应另行支持显式候选节点列表，不能静默改写现有取模语义。

本轮只核对日志、参考源码并记录证据；未修改运行代码，未执行新的 CPU/NPU 测试，
也未提交或推送。本票保持 open，真实服务 KV readback 与大容量验收仍未完成。

### 2026-10-02：按本地 NUMA 节点数和 TP rank 显式分配 BM

用户要求优先处理NUMA分配：新增环境变量，只有显式设置本机节点数后才按TP rank选择节点。
实现`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT=N`，本地节点为`tp_rank % N`，
传入`bm.create2(flags=0x80 | numa_node)`；未设置时仍传`flags=0`。
变量在P/D各自本机读取，不进入peer layout或控制协议的兼容性检查，允许两侧节点数不同。
N须为1..127的整数，假设节点连续编号0..N-1；空值/0/负数/非整数/超范围在BM分配前报错，
不会使用默认策略替代。MF低7位的127保留为自动亲和，当前策略最多选择节点126。

`MempoolKVManager.create()`增加keyword-only `tp_rank`；服务rank-pair入口传真实TP rank，
独立graph/writer/BM gate共享的`verify_graph.run()`传device index作为模拟TP rank。
启用变量却未提供有效TP rank时拒绝创建，避免把P/D BM rank 0/1当作TP rank。
BM创建日志和startup stage记录TP rank、节点数、请求节点及flags；runtime诊断环境快照
也包含新变量。仅决定BM本地DRAM分配位置，容量、CPU绑核及原hostSHM配置不变。

Mac实际检查：

- `test_pair_startup`、`test_pool`、`test_startup_diagnostics`、`test_bm_startup_gate`
  共**28项通过**。新增用例验证1/4/8/127节点数下全部16个TP的P/D SDK flags、NPU ID
  与TP rank不同、默认flags=0、非法配置不分配pool并清理BM context、缺失TP rank拒绝。
- 改动的5个Python文件通过Ruff F/UP037、format及import排序检查。
- 标准strict mypy检查因本机缺少PyTorch报告3处torch import错误；在同一严格配置追加
  `--ignore-missing-imports`后，mempool包及独立gate共9个源码文件通过。
  本轮未重跑依赖PyTorch的完整CPU suite，也未执行NPU测试。

运行说明已补充到mempool模块README和`ascend-mempool-test/README.md`：本机8节点示例
`export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT=8`，重启后TP0/8→NUMA0等；
同时交代unset恢复默认、独立gate继承变量、期望flags/HAL日志及逐节点内存增量核对。
等待用户执行NPU验收，实际物理落点、内存压力下驱动失败/回退行为及大容量启动问题仍未验证。
本票保持open，单请求Graph/正常释放的已有证据不等于NUMA或KV内容验收通过。

### 2026-10-02：小容量真实服务启动、单请求Graph与正常释放闭环通过

用户反馈本轮P/D成功launch，随后curl小学数学题得到正确回答，并粘贴启动与请求日志。
沿用前轮小容量配置context=1024、S_P=S_D=512、CPU亲和性=1；本地已推送入口版本为
sglang `ecf91a4144`、启动脚本 `4f8879e`，未独立读取两台NPU机器的实际HEAD/完整参数。
D TP14的`mapping_ready`确认P_bytes/D_bytes/stride均为1073741824，即每rank各侧
贡献1GiB；全部16 ranks随后完成POOL_HELLO/POOL_READY，并实际处理同一个请求。

启动时序及代码核对：

- P在01:19:02完成本机warmup并输出ready；D日志记录decode graph capture耗时96.94秒，
  01:20:25完成全部16 ranks的control握手，01:20:28完成本机warmup并输出ready。
- `initialize_for_model_runner()`在Graph capture前完成BM映射及runtime安装。
  `MempoolPDService.from_scheduler()`在BM/Graph初始化后接入原Ascend transport，
  其`mapping_ready`日志是接入时再次报告已有映射，不能作为BM刚完成分配的时间。
- HTTP启动warmup携带FAKE_BOOTSTRAP_HOST，mempool service跳过其真实请求ownership。
  因此P可以在D control ready之前完成本机warmup；单侧HTTP ready不代表整个PD链路ready。
- 日志中`smem_trans_entry`、`RegisterLocalMemory Hbm:1`、`memType:DEVICE`及随后
  `IpcOpenMemory`属于原TransferEngine的设备内存注册/连接，不是重新分配DRAM BM。

真实请求身份为room=`8783841730712016187`、attempt=`94e4f933c285d4ed6ab0ff1bbde52c03`，
两侧均slot=0/generation=1。所贴片段覆盖以下全部16 ranks的事实：

| 时间 | 可观察事件与结果 |
| --- | --- |
| 01:28:22 | D acquire_decode/ACQUIRED，发ACQUIRE/BOUND_ACK，free从16到15 |
| 01:28:23 | D收到KV_READY，仍为WAITING_READY，等待原native transfer |
| 01:28:27 | P native_handoff后row_detach（row=16、completed=47）及native_free；P仍WAITING_DONE、free=15 |
| 01:28:27 | D native_transfer_ready后start_decode/DECODING，device0–15均有graph_replay real_requests=1；随后decode batch显示npu graph=True |
| 01:28:40 | D响应200；各rank完成约89ms drain，再row_detach（row=2、completed=128）/native_free，发DONE并恢复free=16 |
| 01:28:40 | P收到DONE后RELEASED、发RELEASE_ACK、free=16；D收到ACK后CLOSED、free=16 |

这验证了当前小容量配置下的单请求正常生命周期、真实Graph replay，以及P原生row资源
早于persistent mempool slot归还的时序。本次可见请求没有遗留占用slot；尚未覆盖连续
复用、零decode、多请求容量压力或失败路径。completed为runtime完成计数，不能替代
实际BM KV字节/数值比对。P片段缺较早的start_prefill/BOUND_ACK/ready，启动片段也未
给出全rank的mapping_ready/graph_captured；没有对这些片段运行完整日志检查器。

用户判断Graph与正常状态转换可用、容量/NUMA和KV正确性仍需验证，与上述证据一致。
容量结论限定为小配置已分配成功：此次同时缩小原hostSHM与BM，不能证明hostSHM是
此前阻塞的唯一原因，也不能确认大容量或指定NUMA分配已解决。当前attention仍消费
原cache/transfer/hostSHM路径，正确回答只验证shadow接入下的输出；服务内mempool
KV内容、跨侧实际top-k读取尚未验收。独立writer已有的合成数据gate不替代这项验证。

下一步保留当前成功服务，执行已有verify_shadow_service.py的三个串行请求（零decode、
实际decode、后续请求复用），等待ACK后检查两侧完整日志并保存requests/lifecycle JSON。
完整shadow gate确认后再添加服务top-k readback。大容量对照与显式NUMA配置继续保留为
待验证事项，ticket保持open。本轮只更新记录与进度表，Mac执行git diff --check；
NPU结果来自用户反馈，agent未执行远端服务或完整日志检查。

### 2026-10-02：CPU亲和性关闭仍卡住，准备小容量服务对照并记录NUMA需求

用户确认D侧`SGLANG_SET_CPU_AFFINITY=0`仍卡住，计划恢复1；这说明关闭开关不足以
解除阻塞，尚不能排除CPU位置对原hostSHM分布的影响。独立BM测试再次由用户确认顺利
结束，NUMA采样从运行中途开始、正常结束后停止；不能把首个采样当作分配前基线。
该时间段的大额分配主要消耗node0/2/4/6，具体驱动节点选择机制未证实。

最新服务快照总Shmem约191.08GiB，独立测试中途约4.50GiB；差值186.57GiB与此前
日志中原hostSHM `12519816192 bytes/rank * 16 = 186.56GiB`接近。最新node6的
Shmem约63.52GiB、MemFree约15.06GiB，node4分别约0.94/115.05GiB。各轮总Shmem
接近但落点变化，尚不足以证明某节点耗尽或页碎片是根因。
宿主机CPU范围依次为node0=0–79至node7=560–639，结合旧绑核日志可知每个TP的
CPU集合跨两个NUMA节点；本轮已关闭亲和性，不能直接套用旧CPU掩码。

按用户计划，将外部启动样例的P/D两段均改为context=1024、S_P=S_D=512，亲和性=1。
日志改存`/tmp/mempool-02-service-small`以保留旧服务目录，样例的请求/检查命令同步更新。
按原78层/17行/context+4/576维BF16布局估算，D原hostSHM变为23.40GiB/机；
BM按现有PoolLayout计算为1GiB/rank、16GiB/机（旧配置6GiB/rank、96GiB/机）。
小容量先用于验证完整服务机制；同时缩小两类存储不能单独证明hostSHM因果关系。
若通过，再保持context=1024、仅恢复S_P/S_D=4096，比较context相关内存压力和BM容量。
context也会影响其他缓冲，仍需根据CONFIG、分配阶段和NUMA数据解释结果。

用户要求后续重视mempool NUMA配置，即使原hostSHM退役后也要保留这一需求：

- [ ] 设计BM本地DRAM的显式NUMA选择，支持各机器/TP按拓扑配置，并记录实际策略。
- [ ] 核实部署版本的Python BM flags、C API及驱动语义；本地MF源码已有
  `SMEM_BM_BIND_NUMA_FLAG_*`和performance flag，以及传入指定NUMA的HAL分支，
  但当前SGLang manager尚未暴露此配置，远端MF 1.1.4二进制的行为尚未验收。
- [ ] 在NPU上核对实际分配落点、内存压力下的失败/回退行为和跨NUMA访问性能；
  CPU绑核不能代替BM内存分配策略。

HostSHM退役时仍须保留/迁移sparse manager承担的HBM sparse cache和top-k读取能力，
本轮继续验证shadow路径。小容量启动、Graph、真实请求和完整生命周期等待用户实测；
ticket保持open，不将缩小配置记录为根因修复。

Mac实际检查：启动脚本`bash -n`通过，两个仓库`git diff --check`通过；直接调用现有
KVLayout/PoolLayout核算上述三组容量，BM贡献分别为6/1/6GiB每rank。没有执行NPU服务。

### 2026-10-01：独立16pair全部通过，复核服务BM建立流程并增加诊断

用户回传两侧run_bm_startup_gate.sh完整runner输出，device0–15各自均PASSED，
两侧均有ALL_BM_STARTUP_CHECKS_PASSED。P PID46144–46159，报告目录
`/tmp/mempool-bm-startup-p/run.fv3xez`；D PID13387–13402，报告目录
`/tmp/mempool-bm-startup-d/run.dKTy4b`。入口来自已推送的927e01e4ef；远端实际HEAD、
逐卡日志/JSON及耗时未独立核验。独立无模型条件下16pair、每卡11GiB、每侧176GiB
同时存活的诊断通过；不能据此认定真实服务启动正常或并发分配与单pair一样快。

按用户要求复核config/layout、ModelRunner/attention backend、runtime和manager调用链。
服务与gate共用create2路径，均只贡献DRAM、HBM=0、SDMA、world size=2；每对P/D
使用BM rank0/1，服务store=base_port+tp_rank，NIC每对预留2端口。layout计算加64字节
probe后按1GiB对齐，使用两侧较大贡献作共同stride；join后校验映射才发布view。
进程内锁只防同worker重复BM初始化，不串行化16个TP进程。未找到可证明本次等待根因的
rank/端口/贡献计算错误。服务的前置模型/NPU KV、D原hostSHM、CPU亲和性与NUMA/cgroup
条件尚与独立gate不同；shadow设计保留原hostSHM，当前没有证据支持删除它或调整分配策略。
另外，两种入口的store端口、pool ID、前置握手和初始化时序也有差异，独立gate不是完整
initialize_rank_pair服务路径的验收。

新增diagnostics.py及环境开关SGLANG_NPU_MEMPOOL_DIAGNOSTICS=1。基本日志统一前缀
`[MEMPOOL_INIT]`，为MF import/init、BM wait_store/init/create2/join/inspect/mappings、
runtime allocate/attach和清理阶段记录BEGIN/END/FAIL、PID/TID及耗时。开启诊断后设置
MF INFO；每15秒对仍未返回的调用记录WAIT和执行线程wchan，首次WAIT附Python/内核栈。
BM创建前后记录RSS、CPU/Mems允许列表、host和NUMA meminfo、由cgroup/mountinfo定位的
v1/v2用量/限制与可见祖先；额外记录布局、原hostSHM总字节数、实际device及相关环境。
映射等待另报missing_rank/GVA/offset。采样线程不调用BM/NPU；诊断信息不可读或监控线程
不能启动时保留原流程，已有SDK异常不被替换。原生调用超时/取消机制没有改变。

本地MF release/1.1的create2绑定释放GIL，因此该路径可在阻塞时由Python线程采样；
远端已报MF commit c01f3ad...不在本地git对象中，未声称逐行核对该二进制。
Docker内核栈权限不足会明确记为unavailable，容器外祖先内存约束仍需宿主机核查。
模块README新增调用流程、分配公式、阶段含义与证据边界；测试README给出原Docker内
开启诊断、沿用此前失败参数重跑服务、先P后D、保留完整日志的步骤。

Mac实际检查：完整CPU suite **124项通过**，含6项新增诊断测试，覆盖SDK实际等待期间
采样正确调用线程、异常退出停止采样并保留原异常、关闭诊断无后台采样、权限/线程创建
失败不阻断、cgroup v1/v2及可见父级限制。严格mypy **19个源文件通过**，ruff lint、
format（40文件）、isort与git diff --check通过。诊断版本尚未执行NPU服务重测，未将
独立BM通过视为服务故障已修复；ticket保持open。本轮改动未暂存、未commit/push。

### 2026-10-01：22:41单pair 11GiB通过，准备独立16pair BM启动诊断

用户回传72层、16slots、S_P=S_D=8192、dim576的writer gate完整P/D终端日志。
两侧device0，P PID45349、D PID12599；local DRAM均为11811160064字节（11GiB），
与服务每rank分配量一致。双方均完成MAPPED、20条PASS（10条decode replay）、
正常释放及ALL_CHECKS_PASSED。JSON和远端checkout未独立核验。
实际MF日志标识版本1.1.4、commit c01f3ad842b9ff7412681a44b67141ce7a124c6d；
驱动V100R001C10SPC009B220。HalMemCreate返回0，P耗时1173193微秒（1.173193秒），
D耗时2005174微秒（2.005174秒）。P在22:41:50.332查询D GVA失败一次，随后
22:41:51.166完成对D段的import/map；这次短暂失败对应对端尚未就绪。
MEMFABRIC_HYBRID_EXTEND_LIB_PATH未设置和libhcom.so未打开也出现在本轮成功路径，
不能仅据此认定它们造成服务启动故障。

结论限于当前Docker/device0/无模型单pair条件：11GiB单次分配可快速成功，未复现
服务D侧create2长时间等待。16进程/多卡、每侧176GiB同时占用、模型加载与D hostSHM、
NUMA/cgroup策略和服务初始化上下文仍是待区分因素，尚不能认定并发死锁或内存不足。

新增verify_bm_startup.py及run_bm_startup_gate.sh，复用原verify_graph.run()的BM
配置、manager create/join和双侧释放流程。每卡申请11GiB，仅写回读64字节peer probe；
本机所选devices全部探针校验成功前不释放池，以覆盖所有池同时存在的条件。
默认并行启动device0–15，两端对应device使用独立store/control/NIC；每次运行生成
新目录，保存每卡log/JSON、PID表和ready标记。支持device子集与dry run；不修改生产
初始化路径，不执行模型KV填充、writer或Graph。README给出容器内两侧命令和判定边界。

Mac实际检查：CPU unittest共118项通过（新增5项覆盖缺失worker/失败probe不会通过、
池等待和两侧16pair端口隔离）；mypy 11个source files、ruff lint/format、isort、
bash -n均通过。新入口--describe确认contributions/stride均为11811160064。
16pair NPU诊断尚未运行，完整服务根因与验收仍未完成，ticket保持open；改动未暂存、
未commit/push。

### 2026-10-01：22:35原writer gate两侧再次通过

用户回传重测终端日志：P PID44599、D PID11856，device0单pair；双方均完成MAPPED、
20条PASS（10条decode replay）及ALL_CHECKS_PASSED。JSON文件及远端commit未独立核验。
P在MAPPED前有一次D GVA转换失败，随后映射成功；本轮不构成持续映射失败。
现有MF/驱动/双机SDMA路径在每侧1GiB的gate条件下可用，不能据此证明服务中的
16pair、每rank11GiB或D已有hostSHM的场景正常。未确认完整服务根因，ticket保持open。

下一轮对照先保持单pair/device0，增大单rank分配至服务相同的11GiB。无需改源码，
直接调用verify_writer.py，保持store/control=18773/18774、pool ID103、dim576、
16slots、24/48-core；使用72层和S_P=S_D=8192这一测试布局。
Mac实际执行该参数的--describe，确认contributions和rank_stride均为11811160064。
这组层数只用于构造相同分配量，不表示模型层数。逻辑读写核验也随布局增大，因此
分开观察MAPPED前的BM创建/映射与MAPPED后的writer检查；MF log-level=1用于观察
SDK分配耗时。11GiB NPU对照尚未执行，未修改生产代码或测试脚本，未add/commit/push。

### 2026-10-01：按用户选择先重跑原writer gate

用户希望在重新启动完整模型前，先复测此前通过的writer gate。核对当前入口：
`run_writer_gate.sh` -> `verify_writer.py` -> `verify_graph.run()` ->
`MempoolKVManager.create()` -> `bm.create2(..., SDMA)`，随后执行同一manager的join、
双向读写验证和decode Graph replay。gate自行初始化BM，未经过service的
`initialize_rank_pair()`，也不创建原SparseKVCacheManager hostSHM。
保留原参数：单pair、每机device0、2层、16slots、S_P=8、S_D=16、dim576；
实际对齐后每rank贡献1GiB。store/control端口18773/18774，pool ID103。
当前模型服务是每侧16个进程、每进程11GiB，两者结果不能直接等同。

Mac实际检查：`bash -n ascend-mempool-test/scripts/run_writer_gate.sh`通过；
`verify_writer.py --describe --s-p 8 --s-d 16 --layers 2 --kv-dim 576 --graph-rows 16`
成功输出两侧各1073741824字节的布局。未修改测试或生产代码，未执行NPU gate。
交付原容器内两端命令，报告另存/tmp/mempool-02-writer-recheck-p和-d，
等待用户回传双方ALL_CHECKS_PASSED及JSON/log结果。真实服务故障继续保持未解决。

### 2026-10-01：D进程等待在devmm分配路径

用户进一步反馈新一轮D TP0 PID=1169，`ps`状态为 `Dl+`，WCHAN为
`devmm_master_alloc_interleaving_`（ps列可能截断）。该采样将等待位置收窄到
驱动内存分配路径，尚不能证明具体页类型、NUMA资源约束或驱动死锁。
主机2.0TiB总内存、217GiB free、1.6TiB available，不支持直接归因为主机总内存
耗尽，但也不能排除特定分配资源不足。numastat未安装；用户消息中的gdb输出为空，
尚未取得原生调用栈。下一步读取该PID的/proc/wchan和/proc/stack，以及NUMA各节点
meminfo和驱动日志；无需安装numastat。仅更新诊断记录，未修改实现或执行NPU测试。

用户补充上述程序和采集命令都在Docker内运行：1169应按容器可见PID处理，宿主机
抓栈前需在该容器进程列表内通过NSpid映射；不能直接对宿主机PID 1169操作。
同时修正内存证据边界：容器内free不能证明该进程拥有1.6TiB可用额度，仍需检查
Docker/cgroup内存上限及允许的NUMA节点。devmm等待位置仍然有效；容器root也不
保证具有读取内核栈或ptrace所需权限，GDB空输出本身不能据此归因为权限错误。
下一步在D宿主机读取容器配置、stats，并对映射后的宿主机PID读取内核栈。

### 2026-10-01：21:23重测定位到D侧create2尚未返回

Codex / GPT-6：用户回传加入阶段日志后的P/D运行记录。两侧全部16个rank的
`bm.initialize()` 均返回0；D在21:24:00–08进入 `bm.create2()`，直到P在
21:25:27末尾被Ctrl-C停止，仍没有D侧 `Mempool BM pool created` 或join日志。
P的TP8在21:23:26完成create2，其余15个rank在21:25:06–10完成；从21:23:18
开始计算，后者耗时约108–112秒。停止P时D的create2仅观察了约79–87秒，
这份记录尚不能区分长时间分配与永久阻塞，也不否定早先运行的超时反馈。

P反复转换失败的 `0x280300000000` 与日志中的D GVA完全一致，P本地base为
`0x280040000000`。D尚未完成create/join时，P获得预留GVA不代表D内存已映射。
21:24:00–08的单次header读取失败对应D的空TCP探测，之后正式initialize成功。
21:25:30开始的D LeaveHandle/inited_、GroupWatch -602及重连失败发生于P停止后，
不能用它们解释此前create2未返回。

核对本地MF `release/1.1` 源码：create2涉及group建立、entity/VA预留、本地DRAM
分配与导出；在910C/GVA V4的VMM分支，host分配优先尝试1GiB页，OOM后回退2MiB页，
随后还有HalMemExport/Import/Map。这是待原生栈确认的候选路径，并非已证明的根因。
日志记录每rank实际贡献11GiB，16个rank每侧共176GiB；D还保留原sparse KV hostSHM。
现有日志没有主机可用DRAM或HAL耗时，不能据此断言内存不足；NPU avail mem不是该指标。

下一步：复现时保持两侧存活，在D侧按新日志PID抓取一个rank的原生全线程调用栈，
同时采集free/numastat，区分HAL分配、导出/映射与内部同步。必要时再用同参数第二份栈
判断是否有进展。当前只记录诊断，无生产代码变更，未执行新的CPU/NPU测试，未add、
commit或push。真实启动问题未解决，ticket保持open。

### 2026-10-01：BM映射查询降频与启动阶段日志

Codex / GPT-6：用户在21:04这一轮重测中反馈，D全部16个rank的P store TCP探测均在
0秒内成功，随后最后一条Python日志仍为 `Initializing mempool BM pair`；P在21:06
仍有多个PID重复报告GVA转换失败。该轮不能继续归因于P store未监听，也不能由这条
调用前日志确定D阻塞在initialize、create2还是join。P日志只证明映射检查尚未通过，
当前片段不足以确定失败地址属于P还是D。真实NPU卡住的根因继续待查。

按用户要求，将 `mempool/manager.py::join()` 的映射查询失败间隔由0.05秒改为1秒，
P/D统一使用，最后一次等待仍受剩余deadline限制；整体mapping timeout不变。
生产改动仅在此文件。补充initialize返回、create2前后、原生join前后和mapping通过的
阶段日志，记录PID/TP rank/device/NIC、实际local DRAM大小、stride及P/D GVA base。
这些日志用于下一次真实运行区分SDK阻塞阶段，不将TCP探测成功误写为BM初始化成功。
README增加阶段定位表和双方日志提取命令；没有调整BM协议、内存布局或控制状态机。

Mac实际执行：原有完整CPU suite **113项通过**；NPU mempool包strict mypy **7个源文件通过**；
Ruff lint/format、isort、`git diff --check`通过。本轮没有为日志或轮询常量新增测试。
尚未执行NPU；等待用户更新两端后沿用原脚本重测，回传阶段日志与最终异常。
降频不等于BM启动卡住已修复，ticket保持open；未add、commit或push。

### 2026-10-01：修复 D 提前完成模型加载时的 BM store 连接失败

Codex / GPT-6：用户反馈真实GLM5.1 shadow启动失败。D在19:05:33连接
`10.120.72.31:19000` 用完60次重试，19:05:34退出；P在19:06:13才进入mempool初始化，
19:06:32开始持续GVA映射重试。两侧日志支持D先退出、P随后等不到D映射的故障链。
核对MF `release/1.1`：BM `PrepareStore()` 没有向 `CreateStoreByUrl()` 传递连接重试次数，
因此初始TCP连接使用默认60次，失败间隔1秒，不受当前 `BmConfig.init_timeout=600` 控制。

实际修改：

- 生产代码仅改 `hardware_backend/npu/mempool/manager.py`。
  `initialize_rank_pair()` 在D进入SDK前调用 `_wait_for_store()`，等待对应的
  `P:base_port+tp_rank`；deadline使用 `--mempool-timeout`，每次连接最多1秒，
  失败轮询间隔最多0.2秒，等待不重置deadline；每30秒报告剩余时间。
  P直接启动自身store。端口可达后仅调用一次正式 `bm.initialize()`；SDK错误直接抛出，
  不将配置/设备故障当作慢启动无限重试。未进入SDK便超时时，不创建pool或保留BM context。
- 探测只建立/关闭TCP，不发送MF header或rank身份。MF 1.1 listener会在登记peer前
  关闭该连接并记录一次header读取失败；真实握手、pool身份和映射校验继续由原流程完成。
  此记录不表示真实MF握手失败，也不能用来忽略后续连续错误。README明确说明该行为。
- `test_pair_startup.py` 新增5项行为回归：模拟P晚90秒才监听、P始终不可达、TCP
  connect消耗剩余deadline、P不等待自身store、可达后SDK错误不重试。先运行原实现，
  慢P场景复现 `BM initialize failed ... -1`；修复后全部通过。
- README补充新日志、timeout边界与用户NPU重测步骤。TCP等待与后续BM/映射分别使用
  timeout；本次没有承诺整个模型加载/服务启动共享同一个600秒总预算。

Mac实际执行：完整CPU suite **113项通过**；NPU mempool包strict mypy **7个源文件通过**；
两个改动Python文件的Ruff lint/format、isort通过，`git diff --check`通过。
未在本机执行MF/NPU服务。等待用户用原 `glm51mempool.sh` 和 `--mempool-timeout 600`
在两端重启完整16对，保留D先完成加载场景，确认wait→reachable→mapping/capture/control
ready，随后继续三个请求与日志gate。ticket保持open，未add、commit或push。

### 2026-10-01：授权推送与GLM5.1启动脚本

Codex / GPT-6：用户授权提交推送本轮part4，并要求基于已跑通的 `glm51dis.sh` 提供
实际启动入口。在相邻 `ascend-sglang-script` 仓库新增
`pd-disaggregation/glm51mempool.sh`，提供 `p/d/router/test/check` 子命令；IP、网卡、
模型路径可通过环境变量覆盖。默认SP/SD=8192、context=16384，保留P eager、D Graph BS16，
追加已实现的mempool server args并保存launch命令、代码版本和完整日志。
顺序为先P、随后D（不等P ready）、双侧ready后router、请求gate、汇总双侧日志检查。
Mac执行bash语法/help及5个子命令的参数/环境构造检查通过（替换Python进程边界，未启动NPU）。
此前108项CPU/静态检查结果沿用，本轮未改动服务实现；真实服务验收仍待用户执行，ticket保持open。

### 2026-10-01：part4 服务接入（工作区，等待用户核对与NPU验收）

Codex / GPT-6：按本票B1–B6及已确认的service＋tick结构实现。未add、commit或push。
本条的代码基线为 `cfcafb4810`；本次工作区包含先前用户writer gate结果的文档记录，
这些记录被保留。B节勾选表示实现及本地检查，Acceptance中硬件项继续保持未勾选。

实际接线：

- 新增 `disaggregation/ascend/mempool_service.py`：真实Req/fake/attempt适配，
  native队列成功接纳后track，固定原bootstrap deadline，batch前安装精确row binding，
  联合readiness，P handoff与row detach，独立pending release，D host/device drain。
- 新增 `disaggregation/ascend/mempool_tick.py`：同侧TP CPU group的3次固定all-gather；
  保留迟到消息，统一plan，control预执行校验，commit后全rank成功才放行outbox。
  所有slot变化均在tick；网络线程只排队。无消息/暂停调度期间也继续tick。
- `mempool/{config,manager,runtime}.py`：启动组合校验、模型尺寸/容量、16对BM启动，
  固定设备表；P/D都挂在attention backend。BM映射在capture前完成，control后创建；
  握手读回实际64字节nonce，不只交换metadata。MF全局生命周期不由单个request关闭。
- `ascend/conn.py`：异步查询原P bootstrap的rank地址，复用原ZMQ reader/PUSH socket，
  配对及HEARTBEAT；native abort复用既有ABORT_ACK，但不使用通用deferred的超时强制free。
- 两个NPU Graph文件和ModelRunner薄scope：独立包装两次warmup、capture、eager和
  每次replay；replay不依赖Python layer hook重新执行。异常保留fatal，不伪造end/completion。
- 共享改动仍只有已批准的7个路径：environ、arg_groups/fields/disagg、scheduler、
  prefill、decode、batch_result_processor、model_runner。未修改mem_cache/common或
  base_prefix_cache；没有新queue子类/第三个接入文件。既有attention writer hook无需改写。

本轮review修正了native取消边界：Failed状态及staging内部finally不能提前归还目的buffer；
ACK tracker在发ABORT前只arm一次；D取消清理保留原metadata room清零和handler unregister；
P sender.clear后不再poll已删除的native status；尚无ACQUIRE的P失败也要先drain。
原grammar/fake取消继续工作，被native intake拒绝的请求不进入acquire。
P完成原handoff后可detach/free native row，persistent slot仍等待D DONE；
D排空delayed sampling/result_queue和设备访问后才free/DONE。等待native ACK不会重复整侧drain。

部署细节：新增环境开关与mempool server args。NIC URL作为基址，每对使用port+2*i，
MF再加本地BM rank；store使用base_port+i。默认容量仍16384，首轮建议用已讨论的8192。
自动retraction/rebootstrap仍不支持；D必须使用新的bootstrap room，拒绝复用已跟踪room。
startup/heartbeat/tick watchdog与native drain使用mempool-timeout；acquire沿用PD bootstrap timeout。
不能确认drain的故障保留ownership并报错，重启须协调停止整个P/D；不自动关闭BM或合成ACK。

Mac实际验证：108项CPU测试通过，21个源文件strict mypy通过；Ruff、format、isort、
AST与diff检查通过。新增测试覆盖16个真实control的统一tick/迟到/preflight故障、容量等待，
service的P row复用且旧slot仍占用、native先到/READY先到、zero-decode、取消后等native ACK、
overlap delayed sampling/result drain和exactly-once回收。少量native seam测试执行源文件的
真实方法体（包含staging finally），只替换系统边界，避免Mac导入NPU依赖。
Standards review无发现，Spec review的问题修正后复核无剩余发现。

交付 `ascend-mempool-test/scripts/verify_shadow_service.py` 和测试README中的启动参数增量。
`requests`通过已有router发送zero-decode、decode、reuse三个请求并保存完整输出；
`check-logs`要求两侧全16 ranks的完整生命周期、各D设备capture/real replay与最终16 free slots。
P/D日志包含room/attempt、两侧slot/generation、native handoff/write-ready、detach、DONE/ACK
以及tick/drain耗时。这里的日志判据不证明KV内容或模型精度。

尚未运行：16对真实BM＋TE共存、真实GLM5.1 server warmup/capture/replay、真实TP collective、
原transfer与mempool联合服务gate。本机没有NPU，以上由用户人工执行。
请按README交付命令回传双方版本/launch命令、完整日志与request/lifecycle JSON。
用户确认无readback的shadow gate后再添加top-k对照；本票保持open，后续依赖票不解锁。

### 2026-10-01：接口优化后的双机 writer gate 回归通过

Codex / GPT-6：用户回传 P/D 两端 2026-10-01 15:52:03–15:52:05 的控制台日志。
本轮交付版本为已推送的 `cfcafb4810`；日志未包含远端 git hash，因此没有独立核验
服务器 checkout。环境沿用此前用户提供的 MF 1.1.4 / Ascend910_9382 等配置，本次
没有新的 check-env 输出。运行入口为上一轮提供的 `run_writer_gate.sh` 双机命令，
P为npu1-31、D为npu1-32，各使用device 0；日志确认pool ID103和控制端口18774。

结果：两端各20条 PASS，其中10条为 decode replay，最后均为 ALL_CHECKS_PASSED。
覆盖24/48 cores；P eager prompt写入、D decode Graph capture/replay写入及对端
逐元素读回；包含chunk prefix、同slot改写、rebind、全invalid和最后一行边界。
每个P数据case核对147456元素，每个D数据case核对294912元素。两端MAPPED输出一致。
D对0x280040000000先发生两次GVA转换重试，之后映射成功；没有持续mapping错误，
没有数值/计数差异、timeout或teardown失败。其余WARN没有阻止本次gate完成。

证据是本会话粘贴的控制台日志；没有收到JSON报告文件，不声称已读取其status/checks。
按交付命令，报告预期位于P的`/tmp/mempool-02-review-p/writer-rank0.{log,json}`和
D的`/tmp/mempool-02-review-d/writer-rank1.{log,json}`，未独立访问服务器文件。

第一部分接口优化的独立writer硬件回归通过。该脚本不验证16对真实TP控制、service
的Req/fake适配、native handoff gate或真实GLM5.1端到端精度。④服务接入、真实服务
shadow与top-k readback仍待完成，ticket02保持open。本次仅更新验收记录/进度文档，
未修改代码、未重新运行本地或NPU测试、未add/commit/push。

### 2026-10-01：第一部分 A1–A4 接口优化（工作区，等待核对）

Codex / GPT-6：按用户要求，先将原四份设计文档 add/commit 为 `d0ace7a5f2`
（Document mempool review refinements and service integration plan）。以下实现保持
unstaged，未再次 add/commit/push；本轮不实现 part4。

- A1：以 `detach_row(binding)` 取代 unbind，返回不可变 `KVWriteReceipt`。
  open forward/该 row 未消费 completion 均拒绝 detach；本地 event 完成后也必须 poll。
  detach 只清 row 映射，P persistent slot 仍由 control 保留。测试由可控调用方模拟
  native handoff 已安全，验证旧 row 可复用、旧 slot 未 DONE 不可 acquire、重复旧
  DONE 不释放新 owner。实际 staging/native cleanup gate 仍须④接线。
- A2：`bind()` 返回本地不可变 `KVRowBinding(row, slot, prompt_tokens)`；service
  按 approved attempt 保存同一个对象。`assert_bound(row, binding)` 检查该对象
  仍是 row 的当前 attachment；相同坐标的新 attachment、缺失 approval、其他 runtime
  的 attachment 和 stale detach 均拒绝。runtime 不解析 Req/fake/session/generation；
  CPU 测试使用 `req.kv.req_pool_idx` 形状，真实 service/fake 过滤仍归④。
- A3：删除 `MempoolWriteInputs` 与兼容分支，只有显式 metadata 的 write 接口；
  保留原 kernel 参数、边界检查、可变行数及 zero-valid launch。同步 CPU 测试与
  `verify_writer.py` 的 bind/detach 调用；双机命令和20条checks/10条replay判据不变。
- A4：`snapshot()` 返回 frozen request/control dataclass、tuple、frozenset；
  包含 readiness、精确 P/D binding、pending DONE/确认消息、实际 local ownership、
  available slots 和 protocol fault。读取不消费 inbox、不推进 phase，不暴露可变 record；
  快照可序列化，旧快照不随控制状态变化。已生成 RELEASE_ACK 的 DONE 不再作为
  pending_done 暴露；内部历史记录保留。协议转换/retirement 算法未改动。
- 已保留固定设备表地址、stream 安装事件和 overlap 计数快照。`KVRowBinding` 只在
  所属进程内按对象身份使用，不重建、不通过 TP 传输；跨 TP observations 使用 control
  snapshot。receipt 按 request attempt 保存，旧 row 复用后不得再以旧 row 查询进度。

Mac 实际验证：完整 CPU suite **83项通过**；严格 mypy **18个源码文件通过**；
Ruff F/UP037、format（31个文件）、isort、`git diff --check` 通过；writer runner
bash syntax、`verify_writer.py --describe` 与本轮文档本地链接检查通过。
TDD 定向回归先观察 detach/assert_bound/snapshot 未实现和必选 metadata 的失败，
再实现并转绿。Review 后补测 DONE 已确认时的 snapshot；最终完整 suite 再次通过。

双轴 code-review 基线为 `d0ace7a5f2` 的工作区 diff（用户要求不暂存本轮实现）：
- Standards：发现1项 P3（生产 mempool README 仍介绍旧 writer 接口），已修复，
  复查剩余0项；未发现代码规范/实质性 smell 问题。
- Spec：同样发现上述1项 README 遗漏，已修复，复查剩余0项；未发现 A1–A4 行为缺陷
  或④ scope creep。真实 Req/fake、native cleanup 和 TP tick 延后符合本票阶段划分。

NPU 本轮未运行；用户需两端同步代码后按 README 的 run_writer_gate.sh 命令回归。
命令、24/48-core配置、两端ALL_CHECKS_PASSED/各20条checks/10条replay判据不变。
历史硬件结果不能视为新接口版本的通过证据。
Ticket02 保持 open，④真实 GLM5.1 服务及之后 top-k readback 均尚未实现/验收。

### 2026-10-01：两部分实现入口与用户确认的 row/slot 释放条件

Codex / GPT-6：用户确认本轮架构复核结论，要求 ticket02 分为已有 part1–part3
代码的复核优化和 part4 服务接入。本次只更新文档；A1–A4、B1–B6 均尚未实现。
保留8个已有生产文件，④只新增 mempool_service.py / mempool_tick.py；保留7个
共享路径的明确薄接口，不新增 integration.py、queue 子类层或通用 backend 框架。

关键确认：KV_READY 后原 KV 仍可通过 staging 传输，因此必须等原 handoff 完成和
本地相关操作排空/无未来 row 使用后，才 detach/free P 的原生资源；P mempool slot
继续等待 D drain/DONE。runtime 不承担协议 ownership，control 提供只读 snapshot。
本轮还确认修正 Req 字段适配、删除 MempoolWriteInputs 双入口和简化不必要的调用约定。

已反馈的硬件证据补记（不是本轮运行）：用户在会话提供两端 2026-09-30 23:27 的
writer gate 日志，与 `0ed22f0aa0` 交付的脚本/cases 对应；未另行核验远端 checkout hash。
P/D 各20条 PASS（其中10条 decode replay），覆盖24/48 cores、P prompt 写/D读、
D decode 写/P读、rebind/边界/哨兵，双方最后均为 ALL_CHECKS_PASSED。
证据为本会话粘贴的控制台日志；JSON report 文件和服务器日志文件未回传，不宣称已读取。
P 启动期 GVA 转换重试随后进入 MAPPED，未阻止 gate 通过。独立 writer/Graph gate
通过不代表16对真实 server/control/drain/readback 已验收，ticket继续 open。

本轮校验：文档内容/相对链接和 git diff whitespace 检查；未运行 CPU/NPU 实现测试，
未修改生产代码，未 add/commit。第一部分的实际回归与第二部分服务验收仍待执行。

### 2026-09-30：③实现交付，等待用户核对与 NPU gate

Codex / GPT-6：按用户要求先提交原工作区内容，提交为 `6dab4b6258`
（Document Ascend mempool implementation plan and verification gates）。
其后③新增/修改保持 unstaged/untracked，没有再次 git add 或 commit。

- `mempool/rows.py` 独立纯张量行推导；`offload.py` 接受可变行数/显式 metadata，
  全 invalid 仍 launch。保留固定 inputs 的已有调用方式。
- `mempool/runtime.py` 提供 `KVWriteExpectation`、bind/unbind/assert_bound、
  begin/write/end/poll 与 writes_done/prompt_ready。固定 request-row 表映射到本侧 slot，
  P 写全 prompt position，D 写相对 decode position；本地完成与协议 ownership 分开。
- eager/capture 验证每层写入一次；replay 通过外部 forward 边界记账。设备有效行计数
  及事件后的快照核对避免全 invalid 静默通过，并覆盖 overlap 的完成顺序。
- backend 增加默认 None 的 runtime 和 attach 防御性校验，extend/decode topk 分支
  在现有 sparse 路径前 shadow 写入；不依赖 save_kv_cache，不改 sparse manager。
  ④仍负责 runtime 创建/attach、scheduler 边界调用、fake marker 和 ownership。
- 新增 `verify_writer.py`、`run_writer_gate.sh`、`writer_cases.py` 及 README。
  gate 与 hook 使用相同 runtime；P prompt 写/D 远端读后，D decode 写/P 远端读。
  这个双向安排替代原 S5 的固定单向机器职责，以实际验证 rank1 的 D 相对位置。
  每个24/48-core阶段都包含 eager、16-row decode capture/replay、rebind、边界和哨兵。
- 双向 gate 收到双方 `WRITER_GATE_DRAINED` 后才关闭 pool；失败缺少 peer drain 时
  保留存储，Ctrl+C 不授权双向 retained pool 的 BM close。01单向 gate 保留原流程。
- 已知重复：rows.py 有意复制 offload_v2，来源/同步维护要求已在文件头、此票和
  design.md 记录；空 batch 保留静态 source extent 为全 invalid，额外 shape 检查
  使 malformed 输入直接报错。后续共享抽取需先补原 manager 特征测试。

Mac 实际执行：完整 CPU suite **77项通过**；严格 mypy 检查 **16个源码文件通过**；
Ruff F/UP037、format、isort、git diff whitespace、两份 gate runner 的 bash syntax 检查通过；
writer `--describe --kv-dim 576` 通过（P/D 各贡献1 GiB，stride=1 GiB）。
backend 的 import/runtime 路径受 torch_npu 限制，Mac 只做静态检查。
新增21项测试覆盖四种布局、可变行数、chunk/local position、容量边界、绑定地址、
安装事件等待、漏/重复layer、计数错误、capture live-binding 拒绝及完整 gate CPU参考。
CPU测试使用 fake SDK/kernel/event 边界，不是实际 BM/NPU 执行。

Standards review 的事件边界/测试 fixture 可读性/嵌套函数介绍已修正；
Spec review 的 live-binding capture 漏记完成和双向失败时 D 提前关闭缺陷已修正。
两项 review 最后定向复核均无剩余发现；Spec review 的 CPU 模拟也确认双向 callback
失败/timeout 时不关闭 BM，以及双向 retention 不因 Ctrl+C 执行 close。
实际 NPU Graph、远端可见性、真实 GLM5.1 server：**未运行**。
③代码与 gate 同轮交付；用户核对、两机 writer gate、④以及服务 readback 都待完成，
ticket02 保持 open，不解锁依赖本票硬件验收的工作。

2026-09-29：用户确认②代码review无疑问并授权commit，已提交
`8d9bdd75b2`（Add Ascend mempool PD control and safe slot retirement），共9个文件，
包括控制实现、conn接入入口、本地测试、README及pool ID修正。本轮未修改代码，
沿用上一轮56项Mac测试与类型/lint结果，提交前staged whitespace检查通过。
用户提供的Claude复核意见已阅读，TP tick/drain/fault及阶段拆分仍需后续设计确认；
②代码核对通过不等于02整票硬件验收完成，ticket保持open，③④待接线。

2026-09-28，Codex / GPT-6，用户确认后的retirement历史修改（未stage/commit）：

- RELEASE_ACK统一确认精确allocation已不再占用P资源，覆盖DONE及安全rollback。
- `_release_slot`在实际归还ownership时更新每slot的`_retired_generation`；
  P/D使用同一退休边界机制，保留session、签名proof、generation和owner校验。
- 移除永久`_released_proofs`集合、`max_release_history`参数及累计65,536次限制。
  近期request records继续受`max_records`约束；仅按slot保存长期退休边界。
- 旧rollback的DONE在CANCELLED记录保留/回收后均返回一致ACK，迟到BOUND_ACK不复活该请求。
  普通unbound CANCEL仍无额外ACK往返，bound取消仍须drain/DONE及P写入排空。
- Mac完整56项测试通过，覆盖65,537次连续释放、有界records、旧rollback与新owner隔离、
  未退休allocation和伪造request/session/slot/proof拒绝。严格mypy检查7个源码文件通过，
  指定Ruff规则、format和diff whitespace检查通过。没有执行NPU测试。
- 同步spec、review和独立测试README；桌面`ascend-mempool-request-lifecycle.md`已更新。
  C4已修复。以下较早记录中“ACK待确认/65,536上限”均为历史状态。③④及硬件验收仍待完成。

2026-09-28，Codex / GPT-6，review缺陷修复（未stage/commit）：

- `_cancel_record`保留DRAINING，重复本地/远端CANCEL不会撤销drain。
- 取消、排空、等待释放ACK或CLOSED时，匹配binding的迟到KV_READY安全忽略；
  不复活请求，冲突slot generation仍拒绝。
- `acquire_decode`在reserve前校验长度类型/容量和reply endpoint，错误输入不占slot/room。
- P收到DONE但BM writes尚未结束时进入CANCELLING，继续保留slot直到写入排空；
  不再把已结束请求对外表示为正常PREFILLING。
- BM startup与PoolPeer统一支持0..255，包含已实测gate的101/102；
  范围依据本地MF release/1.1的TransferEngine entity从256开始，不宣称这是SDK通用上限。

Mac回归先复现原缺陷，再验证修复：完整CPU suite为56项通过，
包含重复CANCEL、迟到KV_READY与新slot owner隔离、非法输入无占用副作用、
pending-write DONE和pool ID边界。严格mypy按`ascend-mempool-test/pyproject.toml`
检查7个源码文件通过；Ruff按仓库pre-commit的F401/F821/UP037规则及format检查通过。
首次未指定配置的广泛Ruff/隔离mypy调用报告旧规则问题与跳过依赖导致的Any返回；
改用仓库/本任务已有检查配置后通过，未为这些无关检查修改代码。

用户明确本轮仅进行Mac回归，不要求NPU测试。未启动NPU/MF/server；ticket保持open。
C4累计release历史上限待用户确认ACK语义；③④接线及其硬件验收仍待完成。

2026-09-28 review: ①存储与②控制已经进行只读复核，现有51项CPU测试通过，
但额外复现发现重复CANCEL导致DRAINING倒退、取消后的迟到KV_READY报错、
无效acquire输入留下slot占用；另有累计release历史上限。
详见[整体方案与代码复核](../design-review-2026-09-28.md)。本轮没有修改实现；
③④接线前应处理这些缺陷并确认drain、故障策略及历史回收合同。Ticket保持open。

任务已建立；第一部分 storage 已实现，其余集成与 NPU 验收尚未完成。

2026-09-27：依赖的 [01](01-mempool-kv-view-graph.md) 已由用户确认验收并关闭；
本 ticket 的 blocker 已解除，可按 `/implement 02` 开始开发。

### 2026-09-27: confirmed delivery scope

- 用户确认按四部分实施：NPU `mempool/` storage；Ascend 控制；attention/稀疏 manager
  双写；conn/scheduler/环境变量与参数接入。
- 第一轮使用真实 GLM-5.1 server，保留原有 main KV transfer、D staging/hostSHM 和
  attention 消费路径。P/D 额外写入 BM；P native HBM cache 保留以服务 prefill。
- 测试可设置 `S_P=S_D=8192` 与较小 context，降低额外 DRAM 需求；产品容量默认仍为
  16384。Graph capture/warmup 的未绑定行保持 invalid，不能占用/访问真实 slot。
- 先交付选项 1 的 shadow 服务运行，由用户在 NPU 验证；随后加入选项 2 的 mempool
  top-k readback 比对，完成整个 02。01 的 fetch Graph 证据不能替代新写入路径的验证。
- 第一部分（Codex / GPT-6）：迁入 01 的 layout 与 BM manager/view，增加 model-derived
  `MempoolConfig` 和 `MempoolKVOffload`。尚未接入 server，不代表 02 控制闭环完成。
  Mac 实际检查：完整 mempool CPU suite 18 项通过；严格 mypy 检查 11 个源码文件通过；
  ruff lint/format、isort、shell syntax 与 diff whitespace 检查通过。默认/不等容量
  `--describe` 通过，独立 import 确认为 runtime 文件且不初始化 SGLang/Torch/MF。
  Standards review / Spec review 均无发现；代码提交为 `a02bdc234e`
  (`Add Ascend mempool storage and graph-compatible KV offload`)，branch 为
  `cryang/dev/mempool`。本地 spec/ticket 保留在 `.scratch/ascend-mempool/`。
  NPU raw-destination offload、服务 capture/replay、shadow 请求和 readback 尚未执行。

### 2026-09-28: rank-pair BM startup helper (unstaged)

按用户确认，在 `MempoolKVManager.initialize_rank_pair()` 内实现 P_i 启动
`base_port+i` store、D_i 连接该 store。两侧分别取 BM rank 0/1，world_size=2；
方法初始化 BM 并 join/验证映射，失败时清理本次上下文。`mf.initialize()` 仍由服务
生命周期拥有，避免 manager 在 TransferEngine 活跃时关闭共享 MF。当前调用方/服务接入
留到第④部分，`POOL_HELLO/READY` 身份校验留到第②部分。
新增 CPU SDK boundary 测试覆盖 16 对 URL/rank 与失败路径；用户要求此次修改保持
unstaged，明确指示后才执行 `git add`。本次无 NPU 执行结果。
复核 MF 1.1 源码发现，BM 尚未初始化时 `bm_rank_id()` 可能返回默认 rank 0，
因此不能用于预检查。已改为 manager 内部跟踪自身创建的活动 BM context，
保留初始化后的 rank 校验；外部 BM context 须由第④部分的启动顺序保证不存在。
Mac 实际检查：新增 5 个 startup 测试及完整 CPU suite 共 23 项通过；严格 mypy
11 个源码文件通过；Ruff lint/format、isort、`git diff --check` 通过。
真实 MF 1.1 的 16 对启动、服务接入和 Graph 路径尚未执行，等待后续阶段的用户 NPU 验收。

### 2026-09-28: KV element width follows declared dtype (unstaged)

Codex / GPT-6：按用户指出的布局可读性问题，`KVLayout` 使用声明的 `dtype` 推导
`element_bytes`，`row_bytes` 与 `byte_offset` 共享该宽度；当前仍仅支持 BF16，
运行时写入继续校验实际 tensor 为 BF16。Mac 实际检查：完整 CPU suite 23 项通过、
严格 mypy 5 个 mempool 源码文件通过、Ruff lint/format 与 `git diff --check` 通过。
没有执行 `git add`，NPU 验证状态不变。

### 2026-09-28: Ascend PD control protocol, phase ② (unstaged)

Codex / GPT-6：新增 `mempool_protocol.py` 的 tagged multipart 消息和严格 schema、
P/D peer 兼容检查；新增 `mempool_control.py` 的单 rank persistent slot/binding 状态、
`KV_READY` 与原 transfer success 双条件、D drain -> `DONE` -> P `RELEASE_ACK`。
`AscendKVManager` 复用现有 PD PULL/PUSH socket；接收线程只排队控制事件，
scheduler 后续处理状态转换。早于 control attach 到达的帧由有界 router 暂存，
坏帧使 mempool 控制进入明确 fault，普通 PD 接收线程继续运行。
取消逻辑区分 D 已绑定与未绑定，P 不会因 `BOUND_ACK` 迟到而提前复用 prompt slot。
近期终态记录有界保留；D slot generation 拒绝回收记录后的迟到 `ACQUIRE`。
P/D slot proof 绑定 request 与两个 lease，结合已释放 generation 校验迟到的
`DONE`/`RELEASE_ACK` 等消息，不触碰新 slot owner。P 还只在实际 `DONE` 释放后
保存精确 release proof；此前经 unbound `CANCEL` 回滚的 binding 不能借后续
slot generation 获得假 `RELEASE_ACK`。release proof 历史上限为 65536，
达到上限时拒绝新 P slot acquire，后续需设计安全的 session rollover/回收。
协议 fault 会阻止新的 P prefill/D decode 工作。

Mac 实际检查：28 项控制单元测试通过；使用隔离 CPU PyTorch 环境运行完整 suite
共 51 项通过。新增两个 runtime 模块严格 mypy、Ruff 与 `git diff --check` 通过。
`conn.py` 在隔离 mypy 配置下仍有 17 项原有的外部模块/旧代码类型错误，
此次新增的 `no-any-return` 已修复。无 NPU 硬件执行。
② 尚无 scheduler/server 接线，③ 双写和④接入完成前，
不能进行真实 16 对 rank 的控制闭环与 Graph 服务验收。未 `git add` 或提交。

### 2026-09-29：D1 设计确认

用户确认同侧统一 acquire/release、一致 preflight 后意外部分 acquire 失败采用
fail-stop，以及首版每 scheduler tick 的 CPU 元数据同步成本。tick 推进持久状态，
不是定时重置。已更新父 spec 和 [接线设计](../d1-d4-d5-design.md)。
D4 drain 和 D5 fault 的具体接入仍待确认；本次仅修改文档，未运行测试，未修改实现。
此决定取代早期“partial acquire rollback 后继续等待”的方案；普通 cancel 的安全
rollback 保留。后续容量/取消 ticket 应遵循父 spec 的新合同。

### 2026-09-29：D4/D5 策略确认

用户接受 D4 首版在 D 侧统一暂停新 batch 提交、排空 overlap 和设备工作、释放结束
请求后恢复其余请求；D5 不可恢复错误直接报错终止，不做同进程恢复。
已同步父 spec 和接线设计。报错不等于 DONE，不允许据此复用未确认安全的 P slot
或销毁 BM。具体 flush、故障传播/timeout 与退出 hooks 仍待③④实现及验证。
本次只更新文档，未修改运行代码或运行测试；未 add/commit。

### 2026-09-29：③④ shadow 接线方案更新

P/D mempool runtime 均从 attention backend 接入，不依赖 sparse manager 的生命周期。
参考现有 sparse UniDexCopy 的设备索引和 Graph 调用，适配 BM slot/position。
③ backend 接线单独完成不等于真实服务 gate；目标 gate 需要④必要启动/control/tick/
drain 接线共同完成，再由用户执行 NPU 验证。服务 readback 仍在首轮运行确认后添加。
详见 [当前实现方案与 review 检查点](../design.md)；已同步父 spec。
本次仅文档更新，没有代码变更、测试执行或 add/commit。

### 2026-09-30：准入 gate 确认与子里程碑

P 在 finalize_bootstrap 副作用前检查 tick 批准结果；D 联合 readiness 覆盖 metadata
和 staging，原 transfer 完成事实独立提交 tick。D1 保留原方案，采纳 D4 idle/pending
release 补充及 MLAPO 禁用约束。父 spec、design、D1/D4/D5 与03/04/07同步更新。
容量由用户 launch 时设置；除用户询问或出现明显相关问题外不反复提醒。

- [x] ① layout/BM manager/view/writer 基础代码已提交（不代表 raw writer NPU 验收）。
- [x] ② 单 rank 协议/control 与 conn 接口已提交（不代表16对服务闭环完成）。
- [ ] ③ backend runtime、P/D temporary KV 双写与 Graph metadata；Mac 检查及代码核对；
  ③整体完成后与下一项 writer gate 同轮交付 NPU 测试。
- [ ] 两机 runtime writer gate：P 写/D 远端读、同 slot 内容更新、capture/replay 内容验证；
  与③同轮 NPU 测试，不单独提前交付。
- [ ] ④ 配置/BM startup/conn/tick/准入/drain/fault 接线；16对启动与绑定。
- [ ] 用户确认无服务内 readback 的真实 GLM5.1 shadow warmup/capture/replay/请求与释放。
- [ ] 加入 top-k 独立 BM readback 对照；用户 NPU 验收并记录证据，才可关闭02。

本次仅更新文档，未修改运行代码、执行测试或 add/commit。

### 2026-09-30：③ S1–S6 实施安排（用户提供）

已将用户安排置于本票前部作为③实施入口：行推导复制到 mempool/rows.py 独立测试，
不改 sparse manager；③整体完成后一次交付 runtime 双机 NPU gate。
已知重复需在两处行推导修改时同步核对，后续补特征测试后再考虑共享抽取。
本次只更新 ticket 和 design 留档，尚未实现③；Mac/NPU 检查均未运行，未 add/commit。

### 历史存档：③ S1–S6 实施安排（2026-09-30）

以下为③代码交付时的计划和状态快照，不是当前实施入口。其中“等待 NPU gate”、
“保留固定 inputs”、“unbind 需 P remote drain”和“assert_bound 直接接收 Req”等
旧描述已由本票前部的当前进度/A1–A4替代；其余已完成行为继续作为回归依据。

<details>
<summary>展开原③计划与交付时勾选状态</summary>

本节是③的当前实施入口，优先于较早的③任务安排。2026-09-30 已完成③代码与
Mac 检查，等待用户核对及两机 NPU gate。S1–S4 勾选仅表示代码/本地检查完成，
不表示硬件验收；完整服务及本票 Acceptance criteria 仍待④。
范围：backend mempool runtime、P/D temporary compact KV shadow 双写、Graph 所需
metadata、Mac 检查和代码核对。不含④的 tick、准入 gate、drain、配置、BM 启动和
真实服务运行。③提供 binding 安装/清除与写入完成接口，不依赖 D1/D4/D5 已实现。

用户确认两点：行推导先在 mempool 内复制、单独测试并留档，以后再考虑合并；
③整体完成后一起交付 NPU 测试，不单独提前交付 writer gate。两机 gate 驱动 runtime
本身，与 backend hook 共用写入路径，不能只测试底层 offloader。③通过仍需④才能
进行服务级验收，ticket02 保持 open。

### 已核对的实现基础

- 现有 `mempool/offload.py` 要求 values 为 `(inputs.rows, heads, dim)`，rows 在
  构造时固定；P eager prefill 的 chunk 行数变化，因此需要扩展。
- `sparsity_driven_kv_offload/manager.py::offload_v2`（当前约656–870行）在每层
  从设备张量推导请求、token position、valid，包含 decode、compact ragged prefill、
  graph 静态 prefill、MoE 尾部 padding。decode 还检查 `seq_lens != 1` 和
  `out_cache_loc >= 0`；原函数尚无直接单元测试保护。
- hook 位于 `AscendAttnBackend.forward_extend` / `forward_decode` 的
  `topk_indices is not None` 分支内、sparse 子分支之前。P 随后走原生
  `forward_sparse`，D 走 `forward_sparsity_driven_kv_offload`。demo 不启用
  MLAPO，此处传入 compact k/k_rope；writer 触发不依赖 save_kv_cache。
- Mac 的 `ascend-mempool-test/src/ascend_mempool/__init__.py` 将 runtime
  `hardware_backend/npu/mempool` 暴露为 `ascend_mempool`，新增模块可用 CPU torch
  测试。backend/attention 依赖 torch_npu，Mac 只做静态检查，hook 必须薄。
- P 脚本使用 `--disable-cuda-graph`；固定 Graph metadata 只要求 D，P 走 eager。

### S1：复制行推导并建立测试

- [x] 新增 `python/sglang/srt/hardware_backend/npu/mempool/rows.py`，纯函数输入
  普通字段/张量：forward mode、req_pool_indices、seq_lens、extend_seq_lens、
  extend_prefix_lens、CPU 侧长度、out_cache_loc 等；不依赖 ForwardBatch 类型。
- [x] 忠实移植 offload_v2 四种布局、有效性条件及错误检查，返回
  `(req_ids, token_pos, valid)`。保留 decode 的 `seq_lens != 1`、cache loc 检查。
- [x] 文件头标明来源函数/对应逻辑，并指向本 ticket 的“已知重复”记录。
- [x] 手算期望值测试四种布局和 padding，不仅用实现自身生成期望值。

### S2：扩展 writer

- [x] `MempoolKVOffload.write` 支持可变行数，接受行推导后得到的
  slots/positions/valid，保持零有效行也 launch。
- [x] P eager 支持实际 chunk 行数；D 使用固定 binding 表，capture/replay 实测待 S5。

### S3：backend mempool runtime

- [x] 新增 `mempool/runtime.py`，按 `layer_id - start_layer` 定位 per-layer offloader。
- [x] 固定地址设备 binding 表 `row_slot`、`row_prompt_len`，大小对应 req_to_token_pool
  行数；第0行作为 graph padding 保持 invalid（-1）。
- [x] 提供 bind/unbind，仅由 tick 批准后调用；③测试用假调用。约束为 bind 对应尚未
  入 batch 的请求、unbind 在 drain 后，操作行不属于任何在飞行 batch。以此作为
  不需要额外 WAR barrier 的设计前提，写成不变量检查，不将推理视为硬件证据。
- [x] `write_layer(layer_id, k, k_rope, forward_batch)`：拼接 compact KV → S1 行推导
  → binding/角色映射 → writer。P local position=pos，要求 pos<prompt_len；
  D local position=pos-prompt_len，要求 local>=0。未绑定行 invalid，仍 launch。
- [x] 同一 stream 写入，与 offload_v2 一致，避免新引入源 tensor 跨流生命周期问题。
  shadow 阶段接受写入位于关键路径，侧流优化留待后续。
- [x] forward 结束时记录完成事件，提供 tick 查询；runtime 不发送 READY/DONE。
- [x] host 侧记账用于首个无服务内 readback gate：每个 forward 每层恰好调用一次，
  总调用数等于本 rank 层数；按期望行数（P extend_seq_lens 之和、D 有效请求数）
  累计 per-slot 写入进度，KV_READY 前与 prompt_len 核对，不一致报错。
- [x] `assert_bound(reqs)`：真实请求必须有 binding，fake 通过既有标识跳过；④在
  run_batch 前调用。底层 invalid mask 不能成为真实请求未绑定时静默通过的理由。
- [x] 构造时防御性拒绝 MLAPO 同开；正式启动配置校验归④。

实现补充：eager/capture 检查 Python layer 覆盖；replay 不执行 Python hook，因此
`begin_forward(..., replay=True)` / `end_forward()` 必须由④在每次真实 replay 外调用。
设备计数按 layer/slot 累计有效行，forward 末尾 clone 快照并记录完成事件；
`poll_completed()` 在事件完成后核对 host 期望值，再更新 completed KV rows。
快照避免 overlap 下早一轮完成事实读到后一轮的计数；计数不代替实际数据 readback。
capture 只允许无 live binding、无 pending completion 的 dummy 状态。
bind/unbind 禁止 open forward，bind 更新在同一 scheduler stream 上提交，下一次
forward stream 等待最新安装事件；unbind 仍需调用方证明远端 drain。

### S4：薄 backend hook

- [x] 在上述 extend/decode 的 topk 分支中调用
  `self.mempool_runtime.write_layer(...)`，不以 save_kv_cache 控制写入。
- [x] `AscendAttnBackend` 提供 attach 接口，默认 runtime 为 None，实际 attach 归④。
- [x] 不修改 sparse manager 的行推导；mempool 不依赖该类的生命周期。
  每个新增类/函数提供简短功能介绍，核心逻辑放入 Mac 可测模块。

### S5：③完成后统一交付两机 NPU 测试包

- [x] 在 `ascend-mempool-test/scripts/` 增加 gate 脚本并更新 README，沿用 run_gate.sh
  风格和 `ALL_CHECKS_PASSED` 判据。gate 驱动 runtime.write_layer，使用合成 batch。
- [x] gate 实现 P 写/D 读的 ragged prefill、chunk 偏移、同 slot 改写和 padding/unbound。
  随后 D 写/P 读 decode capture/replay，改变 slot、prompt length 和位置；使用真实
  BM rank1/runtime D 角色规则，不在 P pool 模拟 decode 相对位置。
- [x] 两侧读端用01读取路径逐元素比较 owner 全部逻辑 KV，覆盖未改动 slot 的哨兵。
  测试脚本已交付；实际双机运行与 Graph/远端可见性证据尚未取得。
- [ ] 按 verification.md 先向用户核对实现、正常路径、ownership、同步/释放前提和
  代码位置，再交付具体双机命令。③与 writer gate 同轮交付，不提前单独验收 writer。
- [ ] 用户执行两机 writer gate，双方20条 checks、其中10条 decode replay，
  `ALL_CHECKS_PASSED` 且报告 `status=passed`；回传日志后核对验收。

### S6：记录与重复代码管理

- [x] ticket Comments 记录实际实现/检查结果；没运行的检查明确写“未运行”。
- [x] ③及两机 writer gate 合并为同一轮 NPU 测试，未验证里程碑不勾选。
- [x] 已知重复：`mempool/rows.py` 已复制自 offload_v2 行推导。修改 padding、
  `seq_lens != 1` 等逻辑时需核对并同步两处；在文件头、ticket Comments 和
  design.md ③一节留档。后续先补特征测试再抽共享纯函数，此次不改无测试保护的
  sparse manager。若发现原逻辑缺陷，先明确差异，不能无记录地令两份代码分歧。

### 检查与交付

按 S1–S4 顺序逐片红→绿，在 Mac CPU torch 环境测试。

- Mac：现有56项 CPU suite 加新增测试；按 ascend-mempool-test/pyproject.toml
  严格 mypy；Ruff F/UP037、format、isort、git diff --check。
- 新增覆盖：四种行布局、P chunk 偏移、D 首次 decode local position=0、
  M==S_D 边界、padding 不写、未绑定不写且记账不一致报错、bind/unbind 后表地址
  不变、可变行数、capture 全 invalid 仍调用 kernel。
- NPU：S5 双机 gate，由用户在③整体完成后手动运行；Graph writer 和远端可见性
  在此之前均没有硬件证据。完整 server 验收仍依赖④。
- 不执行 git add/commit，除非用户另行明确要求。

### 风险与实现核对点

- 复制行推导可能漂移；通过忠实移植、手算测试和留档管理。原 manager 依赖
  torch_npu，Mac 不直接导入对照两份函数。
- backend/attention 的动态路径需要 NPU 验证，Mac 静态检查不代表运行通过。
- bind/unbind 不触及在飞行行是调用方必须兑现的前提，不因同一 stream 写入自动成立。
- Graph replay 不重新执行 capture 时的 Python write_layer；host 记账和完成事件
  必须有每次真实 forward/replay 的调用边界接口，④负责服务接线，③测试显式驱动。
  host 期望行数不证明实际 BM 数值正确，仍须 S5 与后续服务 readback。

</details>
