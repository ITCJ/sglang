# Ascend Mempool Sparse KV PD Demo 设计汇总

## 文档状态

这是本次设计讨论的汇总；正式 spec 已保存为同目录的 [spec.md](spec.md)，
后续开发任务以该 spec 为依据。
用户已确认 Q23、Q24、Q25：acquire 等待纳入现有 PD bootstrap timeout；
demo 暂不支持自动 retraction/rebootstrap；采用下文的精度验收标准。
2026-09-28 复核发现控制状态、释放条件和接入验收仍有需要补充的内容，见
[整体方案与代码复核](design-review-2026-09-28.md)。其中标为“待确认”的建议尚未成为已确认设计。

本文描述待实现的功能；消息名、字段和状态名用于明确协议，不表示仓库已有这些实现。
项目使用本地 Markdown tracker。后续 tickets 放在本 feature 的 `issues/` 目录，
每个任务单独一个文件，并记录状态、验收条件及 blocking edges。
旧的 `agent-mission-track/sglang-npu-develop.md` 不再作为项目维护入口。

## 2026-09-29：ticket02 ③④接线方案（当前 review 入口）

进度：01 已验收，02① storage、②控制协议/单 rank 状态机已提交；
③ backend 数据路径、④服务控制接线尚未实现。以下是方案，不是运行验证结果。
D1/D4/D5 已确认的策略见 [控制与 drain 设计](d1-d4-d5-design.md)。

### Runtime 归属与代码路径

P/D 各自的 `AscendAttnBackend` 持有或引用本进程唯一的 mempool runtime。
Mempool 不依赖 `SparseKVCacheManager` 的创建或生命周期；尤其 P 为
`PD_PREFILL_NATIVE` 时没有 sparse manager，也必须能创建 mempool 并执行双写。
backend 是 forward 接入点；request ownership 决策仍由 Ascend 控制层和统一 tick 负责。

以下路径相对仓库根目录，新增文件名和具体接口仍是实现建议：

| 路径 | 职责 / 拟改动 |
| --- | --- |
| `python/sglang/srt/hardware_backend/npu/mempool/runtime.py`（拟新增） | backend 使用的运行时适配：request-row 到已批准 binding 的映射、forward 写入 metadata、固定 Graph buffer、写入完成观察接口 |
| `python/sglang/srt/hardware_backend/npu/mempool/offload.py` | 复用已有 `MempoolWriteInputs` / `MempoolKVOffload`，按实际 forward 接口补充能力 |
| `python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py` | P/D temporary compact KV 的 mempool 写入入口；eager、capture/replay metadata 接入；避免按已有 host-offload 开关漏掉 P |
| `python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/attention.py`、`manager.py` | 核对 D 当前实际调用链，必要时做小范围适配/共享 compact KV；不让 sparse manager 持有 mempool runtime 或成为 mempool writer 的唯一入口 |
| `python/sglang/srt/disaggregation/ascend/` | conn attach、原 socket 消息传送、统一 TP control tick 适配；协调 scheduler 与 backend runtime |
| PD scheduler 路径、`environ.py`、`arg_groups/fields/disagg.py` | 小范围接线：配置、初始化、准入、binding 更新、drain、正常 release 和 fatal error |
| `ascend-mempool-test/tests/`、`scripts/` | Mac 索引/状态测试，独立 BM 写入 Graph 验证及用户执行的服务验证脚本 |

最终目标是不再依赖原 sparse manager 的 host KV 管理。但当前类还承担 HBM sparse
cache 和 top-k materialization；关闭整个类前必须迁移仍需要的功能。此迁移/最终读取
切换不属于当前 shadow 双写阶段，不能以删除 host KV 为由同时删掉这些能力。

### ③实施安排补充（2026-09-30）

具体 S1–S6 任务、接口、测试矩阵及风险以 [ticket02 的③ Implementation plan](issues/02-rank-pair-control-lifecycle.md)
为实施入口。③不包括④的 tick、准入、drain、配置、BM startup 或服务运行；
③整体完成后统一交付 runtime 两机 NPU gate，不单独提前交付底层 writer gate。

已知重复：计划新增 mempool/rows.py，复制 offload_v2 的行推导并独立测试，本轮不改
sparse manager。修改 padding、seq_lens != 1 等条件时需同步核对两处，文件头和
该 ticket Comments 留档；后续补特征测试后再考虑合并为共享纯函数。
此处记录实施选择，不表示新模块已存在或硬件测试已通过。

### 双写与 Graph 合同

- P：temporary compact KV 同时进入原生 HBM cache 和 P BM；原 transfer 保留。
- D：temporary compact KV 同时进入原 host KV 和 D BM；attention 继续消费原路径。
- 由真实 binding 将 `req_pool_idx` 映射为本侧 mempool slot，不能把两者视为同一编号。
- P 写入位置为 chunk prefix length + chunk 内 offset；D 为全序列 token position
  减 prompt length。首次 D forward 处理 P 采样 token 时，D 本地位置为0。
- 参考 sparse manager 的 UniDexCopy 设备侧 index/valid 构造与 stream 顺序；
  单独处理 mempool binding、P/D 相对位置和容量，不能照搬旧 host KV 布局。
- capture 前准备 BM 映射、baseptr、固定设备 metadata buffer。replay 原地更新
  slot/position/valid，不能固化首次请求的 binding。kernel 源 tensor 遵守 Graph
  内存与临时数据生命周期约束，避免并发 stream 引用已被复用的 KV。
- capture dummy、fake warmup、padding 行均 invalid；零有效行仍捕获写入 kernel。
  真实请求未获得合法 binding 属于准入/接线错误，不能静默当作 dummy。
- metadata 更新和使用必须有顺序，不能在上一轮尚未消费时覆盖；正常请求的容量
  错误由准入拒绝，不能把 kernel 的 bounds mask 当作成功写入。
- runtime 提供写入完成事实；只有统一 tick 可据此推进 KV_READY/release。
  本地完成回调不自行发送 READY/DONE 或释放 slot。P READY 还需远端可读性保证。

### 交付与验证边界

③单独加入 writer/Graph hook，不足以证明真实 server shadow 生命周期正常：
即使 capture 成功，所有行可能仍是 invalid。用户期望的首个服务 gate 需要③及④
必要接线共同完成。按两批代码 review，不能把③局部完成标成整体服务验收完成。

首个 shadow 服务 gate 所需接线：env/args、BM 初始化、backend runtime、conn control
attach、TP tick、真实 acquire/binding、metadata 更新、ready、drain/DONE/release 和
fatal error。D1 同侧统一 acquire/release；D4 暂停 D 新 batch 提交并排空后恢复；
D5 不可恢复错误报错终止，不进行同进程恢复，也不以报错授权不安全的 BM 销毁。

验证顺序：

1. Mac 检查逻辑索引、binding、padding、状态/完成事件；静态检查。
2. 独立 NPU raw-destination 写入与 capture/replay 验证，检查写入后内容。
   01 的 fetch Graph gate 不能代替这个新增 writer 的验证；由用户手动执行。
3. ③④接线完成后，真实 GLM5.1 P/D server warmup/capture、单请求 Graph replay、
   shadow 双写与正常输出；日志确认有效 binding、写入完成、ready、drain 和释放。
   此时暂不执行服务内 BM KV readback，不能据此声称真实请求 BM 数值已正确。
4. 用户确认第3步后，增加独立 top-k BM readback，与原路径的有效 KV 对比；
   readback 不作为 attention 输入。通过后才满足 ticket02 的两阶段验收。

服务脚本基于已跑通的 `ascend-sglang-script/pd-disaggregation/glm51dis.sh` 样例调整，
实际 IP/权重路径以用户机器为准；可使用 S_P=S_D=8192 和较小 context 控制 shadow
内存，必须计入旧 host KV 与 BM 两份 DRAM。默认容量仍为16384。

### 2026-09-30：外部 review 后的确认

- D1 保留原 snapshot all-gather / preflight / commit 设计，首版优先稳定；
  不采用本轮提出的单次 MIN-reduce 及空闲跳过同步优化。
- D4 使用独立 mempool pending release 管理，并纳入 idle/资源检查；
  不复用旧 deferred release 的超时强制释放语义。同轮释放尽量合并一次 drain。
- 首版 mempool 与 MLAPO 同开明确报错；mempool writer 不能只依据 save_kv_cache 触发。
- S_P/S_D 由用户实际 launch 时按机器容量设置。保留容量验证/边界测试要求；
  后续除用户询问或出现明显相关问题外，不反复提醒该参数设置。
- 用户认可两机 writer 内容更新 gate、Graph metadata 顺序、ticket/里程碑整理等
  其余补充方向；具体接口仍需实现时核对。P/D readiness 接入原则已确认，见下节。

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

### 请 review 的实现细节

- GLM5.1 的 MLA preprocessing / `save_kv_cache` 是否造成漏写或重复写；选择可取得
  temporary compact KV 的实际 P/D hook，不只根据函数名假设入口。
- 固定 Graph metadata 在 overlap 下的更新顺序，写入源生命周期和远端可见性。
- runtime 的单实例共享、MF 初始化顺序、capture 前 BM ready，以及 control handshake
  在真实请求准入前完成；这些先后关系不能形成互相等待。
- flush 覆盖 delayed sampling、result queue、零次 decode 和 abort，延迟 req_pool_idx
  回收；TP fault 不能让某个 rank 提前退出而其他 rank 永久等待 collective。

## 1. 目标与范围

在同一 superpod 的两台 16 卡机器上，用 MemFabric BM mempool 和 UniDexCopy
实现 sparse KV 的 PD 分离：prompt KV 保留在 P DRAM，D 按 indexer 选出的
token 位置远端读取；decode 新增 KV 放在 D DRAM。

| 项目 | Demo 约定 |
| --- | --- |
| 模型 | 使用现有本地 GLM-5.1 权重，权重下载不属于本任务 |
| Rank 对应关系 | 固定 `P_i -> D_i`，每端 TP=16、PP=1 |
| Pool 拓扑 | 16 个独立的双 rank pool；每个 pool 中 P 为 rank 0，D 为 rank 1 |
| Store | `P_i` 启动 store，`D_i` 连接 P 的 `base_port+i` |
| KV dtype | BF16，按模型配置读取 compact MLA KV 维度 |
| Physical slots | P/D 各 `B_slots=16`，与 graph padding 独立 |
| Token 容量 | 默认 `S_P=16384`、`S_D=16384`，作为 server args 可配置 |
| Graph | D decode 必须 capture/replay；P prefill 不要求 graph |
| Graph batch width | 最小为 16，满足 GLM-5.1/DeepEP 的 TP 对齐 |
| Prefix / draft | 不使用 prefix 复用，不使用 draft |
| 第一阶段 | 先跑通一个真实请求，随后验证连续请求与 slot 复用 |

每个请求在同一侧的 16 个 rank 上使用相同的 slot ID。
P 和 D 独立 acquire，允许同一请求的 `p_slot != d_slot`。
slot ID 与 SGLang 的 `req_pool_idx` 是不同概念，必须显式映射。

## 2. 启动开关与初始化

启动条件为 `SGLANG_NPU_ENABLE_MEMPOOL=1`，并同时开启 sparse KV offload。
需校验 NPU、Ascend PD transfer backend、P/D 角色和对端配置；配置不兼容应明确报错。
P 当前为 `PD_PREFILL_NATIVE`，不能用 `uses_host_kv_offload=False` 推断 mempool 未启用。

启动顺序：

1. 从模型配置确定本 rank 管理的 layers、KV 维度和 dtype，计算布局与容量。
2. `P_i` 启动 store；P/D 各自创建 BM handle，再加入对应双 rank pool。
3. 在本进程中完成 GVA 到 DVA 的转换，检查映射有效且覆盖所需数据范围。
4. 准备固定地址的 device metadata、sparse cache 和 copy 参数 buffer。
5. D 所有 rank 映射与固定 buffer 初始化完成后，开始 graph capture；warmup 使用 invalid slot/mask，不访问真实请求 KV。
6. 通过现有 PD bootstrap 得到 P 的 ZMQ 地址后，交换并检查角色、rank、协议版本、pool session、布局和容量；首个请求必须等此控制握手完成才准入。

Pool allocation、映射及 DVA 在 graph 使用期间保持有效。退出或重建 pool 前必须排空 graph。

## 3. 内存布局与容量

### 3.1 逻辑布局

一个 rank 的逻辑 KV 布局按 layer 划分，每层为 `[B_slots, S, N, D]`。
`N`、`D` 从实际模型 KV 布局确定；当前 MLA demo 的 `N=1`，
`D` 包含 compact latent KV 与 RoPE key，避免将 index K 算入该区域。
最终路径中，Mempool 替换原有长期 host KV allocation；保留 sparse HBM cache 和 index K。
Ticket 02 是用户确认的 shadow 阶段：保留原有 main-KV transfer、D staging/hostSHM
和 attention 读取，同时向 P/D BM 双写并维护真实 slot 生命周期。因此该阶段会同时
分配旧 host KV 与 BM，需要将两份内存都计入节点预算。先通过无 BM readback 的服务验收，
再增加独立 top-k readback 对照；02 的 BM 读取结果不作为 attention 输入。

```text
row_bytes = N * D * sizeof(BF16)
layer_bytes_P = B_slots * S_P * row_bytes
layer_bytes_D = B_slots * S_D * row_bytes
logical_bytes_side = sum(aligned_layer_bytes_side)
```

例如 `N=1,D=576,B_slots=16,S=16384` 时，一层为 288 MiB。
总容量还需乘本 rank 的实际 layer 数，并包含布局对齐开销。

使用 `create2` 分别贡献 P/D 实际需要的 DRAM：

```text
local_P = align_up(logical_bytes_P, backend_alignment)
local_D = align_up(logical_bytes_D, backend_alignment)
common_max = max(local_P, local_D)
```

两端的 `max_dram_size` 都使用 `common_max`，各自的 `local_dram_size`
可以不同。910C GVA_V4 的 VMM DRAM 后端要求 1 GiB 对齐；其他后端按其要求处理。
物理容量、共同地址步长和逻辑有效范围是三项不同的检查。

UniDexCopy 当前要求单次源/目标逻辑跨度不超过 `UINT32_MAX`、每行不超过 32 KiB。
按 layer 提供稳定的 base pointer；整个 pool 可以大于 4 GiB。
若配置使单层超过当前 kernel 范围，demo 启动时应拒绝，并说明限制。

### 3.2 Mempool KV manager / view

Ascend/NPU manager 封装如下职责，不要求外部逐次查找 rank base pointer：

- 持有 BM handle、各来源的地址映射，以及 shape、dtype、stride、row bytes。
- 提供按 layer、slot、token 访问的逻辑 KV view，并检查有效范围。
- 管理 request 到 P/D slot 的 binding、slot generation 和生命周期。
- 将逻辑 token 位置转换为 UniDexCopy 的 `src_index`、`dst_index`、`valid_mask`。
- 将 batch 中的 `req_pool_idx` 映射到实际 mempool slot，排除 padded rows。
- 对接现有 sparse HBM cache，初始化新请求时清除旧请求的 cache 状态。

`peer_rank_ptr` 返回 GVA；kernel 使用本进程 `gva_to_va(..., LOCAL_DEVICE)` 得到的 DVA。
远端区域并不默认具备 CPU HVA，因此 view 不承诺可由 CPU 直接解引用。
Wire metadata 不传递可被另一进程直接使用的本地 DVA。

逻辑 token 位置的来源判定为：

```text
t < prompt_len:  P slot，local token = t
t >= prompt_len: D slot，local token = t - prompt_len
```

有效性还必须满足实际已写入的 KV 长度。
P 采样出的首个输出 token，其 KV 到 D 首次 forward 处理它时才产生；
已输出 token 数和已写入 decode KV 数不能混用。

### 3.3 HBM 容量与请求准入

Mempool DRAM 容量不代表请求必然能运行。Index K、sparse cache、workspace、
P native KV cache 和模型权重仍消耗 HBM。
准入需同时满足真实 prompt 长度、decode 上限、模型 context 上限及 HBM 预算。
超出容量的请求应明确拒绝；slot 不足的合法请求才进入 acquire 等待。

## 4. 数据路径

### P prefill

1. Request 必须先完成 P 全 rank acquire 和 binding，再进入 prefill。
2. Kernel 产生当前 forward 的临时 compact KV。
3. 保留写入 native HBM KV page cache 的路径，供当前 P attention 使用。
4. 同时从临时 KV buffer 直接写入对应 P mempool slot。
5. 通过 stream/event 管理临时 source buffer 生命周期和全部 mempool 写入。
6. 完成所有 prompt 写入并保证 D 可见后，发布 `KV_READY`。

不在 prefill 结束时重新从 native cache 搬运整段 KV。
原有 handoff Success 可以释放 native HBM pages、清理 sender；
P mempool slot 和持久 request 状态继续保留。

### D decode

Index K 继续位于 HBM，沿用 SGLang 管理和现有 Ascend 传输。
其他需要的 state/aux/metadata 也保留原有路径。
在 mempool 模式中关闭主 compact KV 的原有 PD transfer 及对应 D staging。
接入时必须枚举实际 buffer 清单，防止关闭 KV transfer 时一并遗漏 index K。

D 只有在 `KV_READY` 和原有 index K/state/metadata 传输均成功后，才能运行 decode。
现有 indexer 根据全量 index K 产生 top-k；sparse cache 的 miss 按来源拆分：

- P prompt miss：使用远端 P layer view。
- D decode miss：使用本地 D layer view。

每层分别执行两次 UniDexCopy，汇入 attention 使用的 HBM buffer。
可在不同 stream 并行，但 destination rows 必须不冲突，attention 需等待两路完成。
D 新产生的 KV 直接写入 D mempool；相关读取必须遵守写入完成顺序。

## 5. 正常请求流程与 TP 一致性

1. D 所有 rank 为请求取得同一个 `d_slot`；先统一检查容量，不足则进入 `waiting_acquire_queue`。
2. D 向对应 P rank 发送 acquire 请求；P 独立选择 `p_slot`。
3. P 在一致的 scheduler 顺序中完成所有 rank acquire。容量不足等待；一致 preflight 后意外部分 acquire 失败报错终止。
4. P 全 rank 成功后发送 `ACQUIRED`；D 保存 P/D 对应关系并返回 `BOUND_ACK`。
5. P 等待全 rank acquired、binding 确认及原有输入 metadata 就绪，才调度 prefill。
6. P 发布 `KV_READY`；D 同步确认它和原有传输均成功后进入 decode。
7. Decode 完成或被取消，D 停止后续调度，排空所有相关 graph、copy 和 offload。
8. D 安全释放本地 slot，再发送 `DONE`；保留等待确认所需的轻量控制记录。
9. P 检查消息身份及自身写入已完成，释放 P slot，并返回 `RELEASE_ACK`。

Collective 必须在所有 rank 参与且顺序一致的 scheduler 路径中执行。
ZMQ 接收线程只解析和登记消息，不阻塞等待 slot，也不自行执行 collective。
来自网络线程的 `DONE` 不能无序改变 scheduler 的 free list。
同一侧的 acquire/release 需遵守统一的 rank 调度和状态确认。
P 禁止 optimistic prefill，保持 `optimistic_prefill_attempts=0`。

## 6. 控制消息与身份

复用现有 PD bootstrap、ZMQ endpoint 和 socket，不为 demo 增加独立控制服务。
新增消息使用明确的 mempool tag，由 Ascend manager 拦截，再将其他消息交给原处理逻辑。
同一个 PULL socket 保持单一接收路径。

持久 acquired request table 独立于 sender/receiver 的 handoff 状态表，
通过共享的 `bootstrap_room` 关联两侧请求；`rid` 可用于诊断，不作为唯一跨侧依据。
身份至少包含 pool session/epoch、request attempt 和 slot generation。
每个消息必须验证身份，拒绝上一请求、上一 attempt 或上一启动会话的迟到消息。

| 消息 | 方向 | 主要内容 | 接收方行为 |
| --- | --- | --- | --- |
| `POOL_HELLO` / `POOL_READY` | D -> P / P -> D | 协议版本、角色/rank、session、dtype、layer 布局、S/B、local/max bytes | 完成控制兼容性检查；允许首个请求进入 |
| `ACQUIRE` | D -> P | request identity、d_slot/generation、prompt 长度、decode 上限、reply routing | 入 acquire 队列；重复消息不重复分配 |
| `ACQUIRED` | P -> D | request identity、p_slot/generation、对应 d_slot/generation | 保存 binding 并返回确认 |
| `BOUND_ACK` | D -> P | 完整 binding identity | 标记绑定确认；满足全 rank 条件后允许 prefill |
| `KV_READY` | P -> D | binding identity、实际 prompt KV 长度 | 登记 prompt 可读；继续等待原有 transfer Success |
| `CANCEL` | 双向 | request identity、已知 binding、原因 | 停止新的计算/读取，进入 cancellation/drain |
| `DONE` | D -> P | request identity、已知 binding、完成或取消原因、drain 确认 | 验证身份和 P 写入完成后释放；返回确认 |
| `RELEASE_ACK` | P -> D | request identity、释放结果 | 关闭 pending release 控制记录 |

原有 KV/state/aux metadata、transfer Success/Failed、ABORT/ABORT_ACK 等继续由原协议处理。
原有 `ABORT_ACK` 的含义不能替代 D decode 排空后的 `DONE`。
`KV_READY` 与原有 Success 可以以任意顺序到达，D 采用联合 readiness 条件。

重复 `ACQUIRE`、`ACQUIRED`、`BOUND_ACK`、`DONE` 和 ACK 均须幂等。
已释放的 binding 遇到重复 `DONE`，回复相同终态，不释放当前占用该 slot 的新请求。
终态记录的回收不能破坏迟到消息识别；slot generation 是必要的保护。
ACK 超时不代表对端已经停止访问，也不允许据此强制回收 P slot。

## 7. 状态机

### P 状态

| 状态 | 含义 | 正常下一步 |
| --- | --- | --- |
| `WAITING_ACQUIRE` | 等待全 rank 可用的 slot | `ACQUIRED` |
| `ACQUIRED` | 全 rank acquire 成功，等待 binding 确认 | `BOUND` |
| `BOUND` | D 已确认对应关系，等待调度条件 | `PREFILLING` |
| `PREFILLING` | 生成 KV，双路径写入；slot 禁止复用 | `WAITING_DONE` |
| `WAITING_DONE` | 已发布 KV_READY，prompt KV 保持不变 | `RELEASED` |
| `RELEASED` | D 已 drain，P 已无写入；slot 可复用 | 保留必要的终态记录 |

### D 状态

| 状态 | 含义 | 正常下一步 |
| --- | --- | --- |
| `WAITING_ACQUIRE` | 等待本地 slot | `ACQUIRED` |
| `ACQUIRED` | 本地 slot 取得，等待 P acquire | `BOUND` |
| `BOUND` | 保存对应关系并发送 BOUND_ACK | `WAITING_READY` |
| `WAITING_READY` | 等待 KV_READY 和原有传输成功 | `DECODING` |
| `DECODING` | Graph sparse 读取和新增 KV 写入 | `DRAINING` |
| `DRAINING` | 停止新的调度，等待全部相关读写完成 | `WAITING_RELEASE_ACK` |
| `WAITING_RELEASE_ACK` | 本地 slot 已释放，DONE 已发出 | `CLOSED` |
| `CLOSED` | 收到 P 释放确认，控制流程结束 | 保留必要的终态记录 |

P/D 仍持有 slot 的非终态可进入 `CANCELLING`。
该状态只禁止新增工作，不能直接推出 slot 可释放。
绑定后的取消沿用 D drain -> DONE -> P release -> RELEASE_ACK 的闭环。
绑定前、尚未可能发生读写的局部分配可安全回滚；需保留取消终态，拒绝迟到 ACQUIRE。
Slot 已释放后的控制记录只完成 ACK/终态处理，不能再次操作该 slot 的新 owner。

## 8. Timeout、取消与失败

### Acquire 等待

`waiting_acquire_queue` 纳入现有 PD bootstrap timeout，沿用其配置。
内部转队列和 acquire 重试不能重新开始计时。
Router 超时、客户端断开、原有 transfer Failed 或显式 abort 都可更早触发取消。
需将新增队列和 acquired table 接入这些信号，不能只依赖原 sender/receiver cleanup。

### 安全释放

P native HBM pages 的释放与 P mempool prompt slot 的释放分别处理。
D receiver 在 decode 前被 clear 也不删除长期 binding。
仅收到 `CANCEL` 或看到 HTTP 请求结束，都不能证明 NPU 已停止访问。
D 需要等待最后一次实际读写，包括 overlap 调度中已经提交的额外 forward。
P 也必须等待本地 offload 写入完成。
不使用固定 sleep 或 slot timeout 代替 drain 确认。

普通 PD 的健康跟踪可能随 receiver.clear 移除 room；
mempool 的 peer 检查需覆盖整个 active decode，而不是仅覆盖 handoff。
Peer 失联或 rank 异常时停止使用不可靠映射、明确报告错误；
无法确认安全的 P slot 保持不可复用。Demo 不做自动重连、迁移或强制回收。

### Retraction

Demo 不支持自动 retraction/rebootstrap。
通过 HBM 容量准入减少触发条件；若仍需要 retract，明确终止该请求并走 cancellation。
普通 allocator.free 不能被直接用作 DONE 的发送条件。
控制表的生命周期不能仅绑定 req_pool_idx，因为普通请求资源可能先释放或被重新分配。

## 9. NPU Graph 约束

- Pool、layer base DVA、sparse HBM cache 和 metadata buffer 地址在 capture/replay 期间固定。
- `p_slot`、`d_slot`、prompt 长度、有效 KV 长度及 batch 映射通过固定 device tensor 更新。
- P/D 两路 UniDexCopy 均进入图，不因 capture 时某一路没有有效 row 而省略。
- 每路 `src_index`、`dst_index`、`valid_mask` 固定形状，通过 mask 控制本次实际 copy。
- 真实请求少于 16 时，padded rows 不取得真实 mempool slot，不读写真实请求 KV。
- Attention 等待所需写入和两路 copy 完成；使用多个 stream 时显式建立依赖。
- slot 复用和 req_pool_idx 复用都重置 device sparse cache 映射，避免旧请求 cache hit。
- 同一张图必须能读取不同请求的 slot、索引和长度，不能捕获首次请求的 Python 常量。

## 10. 验收

### 已有证据与待验证项

Ticket 01 已于 2026-09-27 经用户双机测试并确认完成：一个 P/D rank pair、两层，
覆盖相同及不同 DRAM contribution（1 GiB/1 GiB、1 GiB/2 GiB）、24/48 core、
两路来源、动态 slot/index/mask、eager 与 NPU Graph capture/replay，完成逐元素校验。
环境为 MemFabric 1.1.4、torch_npu 2.10.0、sgl_kernel_npu 2026.9.0。
此前 remote DRAM benchmark 与普通 sparse PD UniDexCopy replay 是额外历史证据。

01 使用同步 BM copy staging 后再读取，底层测试 KV 内容在 replay 期间不改变。
尚未证明 02 的 UniDexCopy raw-destination 写入、写后远端可见性、同 slot 内容改写、
16 对 pool 与 TransferEngine 的服务内并存、真实 GLM-5.1 服务生命周期及模型精度。
这些是后续验收项，不能以 01 的通过结果替代。

### 必须通过

1. 使用实际 pool 的双机地址映射与 UniDexCopy 内容校验，覆盖不同 P/D local bytes。
2. Capture/replay 的 KV 与预期逐元素一致，覆盖两路来源、变化的索引和无效 rows。
3. GLM-5.1 单请求完整 PD 路径：acquire、binding、P 双路径、index/meta transfer、D graph、DONE 和释放。
4. 连续请求和 slot 复用；检查 replay 使用新 binding，cache 和 padded rows 没有串请求。
5. 容量不足等待、bootstrap timeout、取消、意外部分 rank acquire 失败时 fail-stop、普通取消安全 rollback、迟到与重复消息。
6. 固定短样例，与现有 sparse PD TransferEngine baseline 的 greedy token 序列一致。
7. AIME26 与 baseline 使用相同权重、请求、sampling 和推理配置，分数不下降；差异必须定位。

现有 AIME26 脚本使用 `temperature=0`、`max_tokens=28672`。
正式验收相应设置 `S_D`，并检查 prompt + decode 的 context/index K HBM 预算。
16384 的默认 S_D 不能直接覆盖该评测上限。

先保证数据、图和生命周期正确；性能结果记录实际测量值，不预设加速幅度。

## 11. 开发边界与建议顺序

核心布局、协议、slot 状态、offload 和 sparse fetch 尽量放在 Ascend/NPU 目录。
优先复用现有 manager、backend 和接收回调入口；不额外创建同 socket 的 recv 线程。
共享代码仅增加必要的 server args 或生命周期接入，具体 hook 需在开发任务中核实。
不修改与本 demo 无关的主线行为。历史开发基线为 `295132c4a5` / upstream PR #33089。

后续适合拆为可验收的阶段：

1. 双机 BM + UniDexCopy graph 验证，先消除跨节点 capture/replay 的硬件不确定性。
2. 单请求纵向贯通到 DONE/RELEASE_ACK，含启动配置、P/D 数据和现有 metadata 路径。
3. 连续请求、slot 复用和异常生命周期，验证队列、TP 同步及消息幂等。
4. 同配置精度对比和 AIME26 验收，再记录性能。

未来 NUMA 内共享 MLA KV、不同 P:D 数量比、prefix 复用、draft、
自动恢复/retraction、移除 P native KV cache 均作为独立后续工作。
