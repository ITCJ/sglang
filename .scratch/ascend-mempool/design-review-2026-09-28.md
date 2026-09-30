# Ascend mempool：整体方案与已实现代码复核

日期：2026-09-28。范围：ticket 01、02①存储、02②控制，以及后续完整服务接入方案。
本次只修改设计文档；没有修改、stage 或提交实现代码。
已确认的产品约束仍以 [spec](spec.md) 为准；本文的待确认建议不自动替代原协议。

后续修复状态（2026-09-28）：用户授权修复已确认的代码问题。C1/C2/C3已修复，
P在收到DONE但写入未排空时改为CANCELLING；C5的服务BM namespace已统一为0..255，
避开MF 1.1从256开始的TransferEngine entity范围。56项Mac CPU测试、配置指定的
严格mypy和仓库pre-commit所选Ruff规则通过。下文保留原review依据。
C4后续已获用户确认并修复：统一记录DONE与安全rollback的retired generation，
保留session/proof/owner校验，删除累计65,536次限制；历史review描述保留供追溯。
③④服务接线、TP tick、BM/ZMQ关联和真实NPU验收均未由本次修复完成。

## 1. 进度与总体判断

| 阶段 | 当前状态 | 此次判断 |
| --- | --- | --- |
| 01：独立 BM + sparse copy Graph | 用户已完成双机验收 | 保持完成；不重新打开已通过的 gate |
| 02①：`hardware_backend/npu/mempool/` | layout、view、manager、write inputs/offload 已实现 | 未发现确定性的高优先级寻址错误；raw-dst 写入尚缺硬件证据 |
| 02②：`disaggregation/ascend/` | protocol、control、既有 ZMQ reader 分流已实现，未接入真实 request | 正常路径已有测试；取消/迟到消息存在可复现缺陷 |
| 02③：NPU attention / sparse manager | 待接入 | 需明确 P/D 初始化、temporary KV、Graph metadata 和 stream 顺序 |
| 02④：Ascend conn / PD scheduler / 配置 | 待接入 | 需落实 TP control tick、warmup、drain、失败处理及真实服务 gate |

总体架构可保留：16 个独立 pair pool；P prompt KV 留在 P；D 新 KV 留在 D；
Index K 和必要的 state/aux/meta 保持原管理及传输；固定地址 metadata 支持 Graph。
问题集中在控制协议的并发边界与服务接入合同，而非需要推翻 BM 布局。

## 2. 必须保留的既定决定

- 两机同 superpod；TP16、PP1、BF16 compact MLA；无 prefix/draft/自动恢复。
- 每 pair 内 P=rank0、D=rank1；P store 使用 base_port+i。
- P/D 独立 acquire，允许 p_slot != d_slot；同侧 TP 使用同一 slot ID。
- B_slots=16 与 Graph padded width=16 是不同维度；默认 S_P/S_D=16384 可配置。
- BM mappings、DVA 和固定 Graph buffers 在 capture 前就绪；ZMQ 控制握手在首个真实请求准入前完成。
- P native HBM KV cache 保留以服务 prefill；P BM slot 生命周期延续到 D 安全结束。
- 02 先 shadow 双写、不做 BM readback；用户验收后加入实际 top-k 的独立 BM 对照读取。
- 最终路径仅关闭 main compact-KV transfer/staging/长期 host KV；不关闭 Index K/state/aux/meta。
- acquire waiting 计入既有 PD bootstrap timeout；未知是否安全的 slot 不强制回收。
- 实际 NPU 运行由用户执行；Mac 检查不替代硬件验收。

## 3. 已确认的代码问题

以下路径相对仓库根目录。P1 表示应在③④接入前修复；本次没有修复。

### C1 / P1：重复 CANCEL 使 DRAINING 状态倒退

位置：[mempool_control.py](../../python/sglang/srt/disaggregation/ascend/mempool_control.py)，
`_cancel_record()`，当前约 691–706 行。

复现：P 发送 CANCEL → D 接收并 begin_drain → D 再次收到同一 CANCEL。
状态从 DRAINING 回到 CANCELLING，随后 finish_drain 抛出
`D request has not drained`。这不需要消息伪造或跨连接乱序，重复发送即可触发。

建议：同一 binding 的 CANCEL 对 DRAINING 幂等，不撤销已经开始的 drain。
测试覆盖本地取消、远端取消、重复取消与 drain 完成回调的交错。

### C2 / P1：取消后到达的合法 KV_READY 被当作异常

位置：同文件 `_accept_kv_ready()`，当前约 549–570 行。

P 已写完并发出 KV_READY，但 D 在收到它之前取消请求。消息在
CANCELLING、DRAINING、WAITING_RELEASE_ACK 或 CLOSED 到达时会抛出
`KV_READY arrived outside the active binding`。记录被 eviction 后，同样的旧消息反而可被忽略。

建议：先核对 binding；已经取消/结束的同一 binding 不重新激活，也不把正常迟到当成协议损坏。
冲突身份仍应拒绝。增加 readiness 与 cancellation 双向交错、终态保留/eviction 两组测试。

### C3 / P2：acquire_decode 参数错误会留下已占用 slot

位置：同文件 `acquire_decode()`，当前约 203–241 行。

`prompt_tokens=1.5` 或 `reply_to=""` 在返回消息的 schema 检查中被拒绝，
但 reserve、record 和 room owner 已经写入。实际复现 free slots 从 16 变成 15。

建议：所有可能失败的输入验证在 ownership mutation 之前完成，或明确事务回滚。
这属于 API 的异常安全问题，不代表普通合法请求必然遇到。

### C4 / P2：累计 65,536 次 release 后永久停止 acquire

位置：同文件 `acquire_prefill()` 约 257–262 行、`_release_prefill()` 约 677–689 行。

`_released_proofs` 为每次正常 DONE release 保留 proof，默认上限 65,536。
达到上限后，即使所有 physical slots 都空闲，新的 acquire 仍被拒绝。
这不是并发容量限制，也不是 MF 限制，而是当前控制历史的服务寿命限制。

建议在继续扩展协议前确认 RELEASE_ACK 的语义与历史回收机制，见 D3。
仅增大上限会推迟问题；不应把此限制隐含在一个可持续服务的 manager 中。

### C5 / P3：pool ID 范围与独立测试入口不一致

`MempoolKVManager.initialize_rank_pair()` 与 `PoolPeer` 限制 pool ID 在 0..63，
而 01 的 run_gate 使用 101/102 且已经实测通过。检查本地 MF release/1.1 后，
未找到把 0..63 解释为 SDK 必需限制的依据。默认 pool ID=0 不受影响。

建议统一项目 namespace，或明确这是服务路径主动保留的范围，不是 SDK 通用上限。
同时考虑与 TransferEngine entity namespace 的关系，不应简单开放所有整数。

### 依赖后续 transport 合同的事项

- DONE 在 P ACQUIRED 时被拒绝。只有允许应用层将 DONE 排在 BOUND_ACK/CANCEL 前发送/处理时，
  这才成为正常路径缺陷；不能把同一有序 ZMQ sender 的 FIFO 描述成任意网络乱序。
  后续应明确每 pair 单一发送队列、重试规则与允许的消息交错，再决定接受状态。
- P 收到 DONE 且 writes_pending=True 时保存 pending_done，但 phase 仍是 PREFILLING。
  当前没有发现因此提前释放；不过③④必须阻止再调度下一个 chunk，不能只凭 PREFILLING 决定可运行。
- 部分 rank acquire 后的 rollback 会结束当前 attempt；重试需全 TP 一致的新 attempt，
  或提供尚未发布消息的 local reserve/rollback 阶段。不能各 rank 自己 cancel 后继续旧 attempt。

## 4. 整体方案需要补充的接入合同

### D1：TP 同步需要固定推进顺序

“在 scheduler 中做 collective”还不够。每个 rank 的网络到达顺序不同，如果各自
遍历 inbox 然后调用 collective，可能对不同 request 做同一次 collective，甚至调用次数不同。

建议：在双方 scheduler 的一致位置执行 control tick。按所有 rank 可对齐的逻辑
request key 汇总候选、readiness 和 free-slot bitmap，再按同一顺序做决定。
逻辑 key 可采用 `(bootstrap_room, attempt)`；attempt 由同侧 leader 产生并广播，
P 沿用 D 请求携带的 attempt。完整 wire identity 中的 p_session/d_session/proof
属于 rank pair，不能拿它们要求同侧 16 ranks 全部相等。

tick 在没有新 request 时也必须运行，否则最后一个请求的 DONE/ACK 可能无法处理。
网络线程只解析/排队；不在其中做 TP collective 或直接调度模型。
参考 `disaggregation/prefill.py` 的空 bootstrap queue 提前返回路径。

### D2：BM peer 与 ZMQ peer 必须关联验证

当前 BM 根据 store endpoint 配对，ZMQ 根据 PD bootstrap endpoint 配对；
现有握手只比较配置与独立 startup sessions。两个配置相同的 deployment 接错时，
可能 BM 指向 A、控制指向 B，仍通过布局校验。

建议复用 64-byte probe 区传 startup 随机 nonce：通过 BM 写入对端，
对端实际读取后通过 ZMQ 回报并核对。两端都验证，且在 control-ready 前完成。
部署 UUID 可用于诊断，但仅配置相同 UUID 不能证明两条连接确实指向同一进程。
现有 probe_peer 只有写入动作；01 的 PROBED 交换没有验证 marker 内容，不能视为此身份验证已完成。
这不改变“映射先于 capture、控制握手先于请求”的启动决定。

### D3：RELEASE_ACK 与历史回收（已确认并实现）

用户已确认 RELEASE_ACK 为：**该精确 allocation/binding 已不再持有 P 资源**。
正常DONE与安全unbound rollback均通过实际slot释放推进`_retired_generation`。
每个slot串行复用，因此边界以下的已签发allocation都已经结束；session、签名proof、
D generation和当前owner共同校验，未知record或单独generation比较不能作为释放证据。
近期终态记录有界保留，移除`_released_proofs`永久集合和累计请求上限。
普通rollback仍不新增ACK往返；匹配的旧DONE可在rollback record保留和回收后得到相同ACK。
Mac回归包含65,537次释放、伪造身份/slot/proof、retained/evicted rollback与新owner隔离。

### D4：Drain 不仅是一次 device synchronize（待确认首版策略）

实际 decode overlap 会先提交当前 batch，再处理上一 batch 的完成结果。
普通 release_kv_cache 还可能先归还 req_pool_idx。mempool 不能只在收到完成结果时
调用 synchronize 后释放，因为需要排除 host 上尚待提交的旧 attempt 工作，以及旧 binding 的后续读取。

必要顺序：禁止该 attempt 新 submission → 处理已排队和已提交的引用 → 等待 NPU 读写完成
→ invalidate 旧 device binding/cache → 归还 req row 与 D slot → DONE。
P 收到 DONE 后还要等待自己尚未完成的写入，才能归还 P slot。

建议 demo 首版允许保守地 flush overlap work 并同步，再考虑按 request 的 submission
sequence/refcount/events 优化。这里的 flush 指准确排空相关工作及引用，不是清空队列并丢弃工作。
该同步可能拖慢其他请求，需要用户确认这个性能取舍。

### D5：请求失败与 pool 故障要有不同退出路径（待确认）

普通 timeout/abort 应取消该 request，并在可证明安全时释放；不能直接复用未知状态的 slot。
目前 router/control 的 sticky fault 会拒绝后续全部 tagged 消息，包含 DONE/ACK，
因此它不是“仅停止新准入、旧请求仍能正常清理”。

建议首版把映射丢失、peer session 不匹配、不可恢复协议错误等归为 pool/worker fatal fault：
向同侧 TP 一致传播、停止服务，保留无法证明安全的远端 slot，按受控退出处理。
不尝试自动重新映射或 reconnect；peer 故障检测仍需覆盖 receiver 已清理后的 active decode。
这里的 fail-stop 首先指停止新准入/新提交，不是立即销毁 BM：只排空本地设备不足以证明
对端已停止远端读取。正常受控销毁仍需双侧停止访问的确认；peer 已失联的故障处置需要明确
双端停止/人工重启顺序，不能把本地 close 当作远端安全释放证明。
若希望同进程继续服务其他请求，就需要另一套明确的 degraded/cleanup 状态，不能依靠当前 sticky fault。

### D6：P 初始化、warmup 与零次 decode 路径

- P 的 PD_PREFILL_NATIVE 不创建 SparseKVCacheManager，因此“sparse manager 初始化后再创建
  mempool”只适用于 D。应在 NPU backend 共同初始化路径创建 storage，仍保证 capture 前可用。
- HTTP startup warmup/health 会发真实 FAKE_BOOTSTRAP_HOST 请求，不只是 Graph capture
  的 padded rows。使用既有 fake-request 标识；不能只凭 bootstrap_room=0 判断，因为它并非专用身份。
  建议这些内部假请求不 acquire、不发送真实控制消息，metadata 无效；shadow 保留既有路径。
  正式 cutover 后需单独验证 fake 请求可完成，不能假定 zero-valid copy 本身保证 attention 正常。
- max_new_tokens=1 或 handoff token 已命中 EOS 时，可能在 prebuilt 阶段直接完成，
  完全没有 decode forward。需要正常 release 路径；可在 readiness 完成后逻辑进入 DECODING
  再做零工作 drain，或增加明确的 ready-to-drain 状态，不能把正常结束强制标成取消。
- prompt length、生成 token 数、实际已写入 D KV 行数不同。首个 P 生成的 token 何时
  在 D 产生 KV、chunk offset 如何累积、当前 token 写入如何先于本步 fetch，必须以实际
  forward tensors/positions 为依据，不能仅按输出 token 计数推导地址。

### D7：硬件证据与内存预算

01 证明的是同步 staging 后的跨节点 Graph 读取。02 新增 raw dst_ptr writer，
需要验证 source lifetime、stream 依赖、写完成到远端可读的保证、slot 内容改写可见性。
allocator 的 record_stream 只保护存储生命周期，不等于 producer/consumer event。

建议补一个独立 writer/readback gate，减少遇到问题时必须启动完整 GLM 服务的成本。
这不会改变 02 服务验收先无 readback、再 top-k readback 的两步顺序。

每 rank BM 预算为 `layers * 16 * S_side * heads * dim * 2`，加 probe 和 alignment。
节点预算要汇总实际 16 ranks 的 contribution，并计入 shadow 的旧 hostSHM、模型及其他 host 内存。
同侧 MLA 的 16 份复制是本 demo 已接受的代价；未来 NUMA 共享不属于本轮。
Index K HBM/context 限制仍独立检查；S_P/S_D 能分配不代表整个服务能接纳该长度。

## 5. 建议的任务边界校正

旧 ticket03/04 写于 02 扩展前，仍称“首次真实 P offload”“首次完整模型 demo”。
这与已经确认的 02 shadow 服务集成重叠。建议按用户此前的方向整理为：

| Ticket | 交付边界 |
| --- | --- |
| 01 | 已完成的独立 BM/Graph 数据读取验证 |
| 02 | 当前四个实现部分；真实 server shadow 双写及生命周期，先无 BM readback，再 top-k 对照 |
| 03 | 正式切换 D 的 mempool sparse fetch；选择性关闭 main-KV transfer/staging/旧 host KV，保留 Index K/meta |
| 04 | 正式路径的单请求 Graph、warmup/health、零次 decode、chunked prefill及 baseline 对照集成验收 |
| 05–08 | 保留各 ticket 的后续生命周期/容量/精度目标；在03/04边界确认后再核对依赖 |

这是边界调整建议，不表示03/04完成或可以绕过02硬件验收。尚未重写旧 ticket 的全部验收清单。

## 6. 检查记录与后续验证

本轮 Mac 实际执行：

```sh
PYTHONPATH=ascend-mempool-test/src /tmp/ascend-mempool-01-venv/bin/python -m unittest discover -s ascend-mempool-test/tests/unit
```

结果：51 tests passed（包含28项控制测试）。CPU torch 的缺 NumPy warning 不影响本轮结果。
独立内存复现额外确认 C1、C2、C3；现有测试通过不能覆盖这些遗漏。
没有新建或修改测试文件。没有执行 NPU、16-pair 或实际模型测试。

建议后续补充的 gate：

| 层次 | 必须观察到的行为 |
| --- | --- |
| CPU control | 重复 CANCEL 不回退 drain；迟到 KV_READY 不复活或故障；无效 acquire 不留副作用 |
| CPU control | 历史 eviction、同slot新generation、rollback/retry、ACK丢失/重发；超过旧历史上限仍能持续服务 |
| TP integration | 不同rank事件到达顺序、空队列期间DONE、partial acquire failure及统一attempt重试 |
| Startup | 16 pairs + TransferEngine并存；故意交叉BM/ZMQ deployment被拒绝；正确启动不依赖握手先于capture |
| Writer hardware | invalid capture→valid replay实际写入；同slot换内容；P写后D远端读；D当前token写后fetch |
| Server shadow | warmup/health；一个请求；零次decode；多chunk；结束/abort下overlap drain和slot复用 |
| Shadow readback | 按实际top-k有效行逐元素对照；prompt/decode边界、不同P/D slot、padding与短写 |
| Formal cutover | 证明main-KV流量/旧hostKV已关闭，同时Index K/state/aux/meta仍正确；短样例及AIME26基线对照 |

当前①②尚未提供上述新增 gate 的可直接运行脚本。脚本、参数和逐机命令应随对应实现交付，
不能把未实现的 CLI 写成现在可执行的指令。

## 7. 本轮设计问题树

已定根节点：拓扑、数据放置、Graph时序、shadow阶段、原传输保留范围。

本轮可独立确认的 frontier：

1. 已确认并实现：RELEASE_ACK表达“精确allocation已不再占用P资源”，采用有界历史回收。
2. 首版是否接受完成/取消时保守flush overlap + 同步，再优化按request排空？
3. 不可恢复pool/control故障是否采用同侧TP一致fail-stop、人工重启？
4. 是否将03/04调整为正式数据路径cutover与其集成验收，消除与02的重复？

确认后下一层：历史回收的具体不变量、drain hook和状态表、fault传播与退出协议、
03/04验收清单。TP推进顺序、BM/ZMQ身份关联和额外硬件gate可作为既有正确性目标的实现细化，
后续仍需代码review与实测，不以本文件替代。
