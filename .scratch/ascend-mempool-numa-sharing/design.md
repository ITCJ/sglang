# Ascend mempool NUMA 共享：设计讨论

**Type:** design discussion
**Status:** ready-for-agent
**State:** open

更新：2026-10-12。代码依据：`cryang/dev/mempool`，`cc10a7b794`。
**2026-10-14更新：** 用户已接受组件01完成，02可以开始。生产分组改为先确定
NUMA分配，再由share开关决定同机同节点合池或每rank独立；默认每节点两个
ranks，实际成员数可配置。新合同见[执行设计第0节](execution-design.md)。
下文保留2026-10-12的调查背景；其中“尚未实测”与“两成员首版限制”为历史状态。
这是新需求的讨论与证据记录；[规格](spec.md)和[交付计划](delivery-plan.md)
已按用户对三阶段的反馈发布。详细基准与并行依赖见[执行设计](execution-design.md)。
尚未实施或通过硬件验收。
用户已在同日明确确认初步设计完成；本文作为设计依据保留，后续以规格、执行
设计和01→02→03的实现验收推进，不再重复已确认的需求问答。
现有 demo 合同见[原 spec](../ascend-mempool/spec.md)；
术语沿用[CONTEXT.md](../../CONTEXT.md)。

## 用户目标与已确认决定

- 用户提出：减少 MLA/GQA 的冗余 mempool KV replica，同时利用 NUMA 和 NPU
  互联带宽。示例为 TP16、8 个 NUMA，每个 NUMA 的池服务两个 TP ranks，
  两个 ranks 分工写入，避免重复写。
- 用户澄清：3M 指 decode 每张 NPU 的最大 Index K token 容量，
  不表示单请求长度或 mempool 已实现相同的常驻 KV 容量。
- Q1 已确认：先在相同 token 负载下保持 decode 延迟和吞吐，再评估节省内存后
  能扩大的容量。具体性能门槛见 Q7；工作负载矩阵与统计方法仍需落到测试计划。
- Q2 已确认：首版先完成 MLA；GQA 不纳入首版验收。
- Q3 已确认：写入分工粒度通过性能测试选择，不要求每层每步两 ranks 各写半行。
- Q5 已确认：本步新 KV 从各 rank 本地供给，历史 KV 共享，复用 attention 后
  已有的 TP 通信完成发布，不新增独立跨 rank barrier。最初保留 producer
  stream；用户随后要求细化并行路径，现将同 stream 作为对照，允许提前提交
  的独立 write stream，在既有 TP 前接入本地完成事件。仍需共享 BM 内容、
  可见性和 Graph 验证。
  理由见 [ADR 0001](../../docs/adr/0001-mla-shared-kv-publication.md)。
- Q6 已确认：P/D 使用相同的 TP rank 分组，两侧分别配置本机 NUMA；
  首先验收每组 2 ranks。同组 P ranks 共享 prompt KV，同组 D ranks 共享
  decode KV，沿用现有 `P_i ↔ D_i` 控制对应关系。
- Q7 已确认：相同模型、输入、并发和 Graph 配置，多轮对照后 decode 吞吐、
  每 token 延迟的中位数及 P95 均允许最多 3% 回退，容量扩大单独测量。
  门槛分别为 `throughput_shared >= 0.97 * throughput_baseline`，
  `ITL_p50_shared <= 1.03 * ITL_p50_baseline`，
  `ITL_p95_shared <= 1.03 * ITL_p95_baseline`。
- Q8 已确认：首版先验证去重与性能，不硬顶 3M 或机器容量上限。
  实际 2T 机器拆到每个 NUMA 后可分配多少 BM 内存，留待实机测量；
  不以总内存平均值、MemFree 或虚拟映射空间推断可分配容量。
- Q9 已确认：先做 NUMA 共享，再衔接原 mempool ticket04。
- 三阶段核对反馈：01 必须先比较两个独立 mempool／一个共享 mempool 的双
  rank 同时写入和完整读取性能；02 设计更细并尽量并行，避免影响性能；03 无异议。
  据此发布三票，服务最终验收仍使用此前确认的 3% 容差。
- 最新补充已确认：current KV 填入 selected buffer 与历史 UniDexCopy 并行；
  新 KV 写回可覆盖“同一步的下一层 attention”。采用一层滞后候选，
  write(L) 在 TP(L+1) 前完成，末层已有 TP 前收齐，不跨 decode step。

## 已查明的边界

1. **当前 NUMA 参数只改变分配落点。** `manager.py` 按
   `tp_rank % len(numa_nodes)` 选择节点，每个 TP worker 仍独立分配；每对
   `P_i/D_i` 建立 `world_size=2` 的 BM session。若使用列表 `0..7`，同节点
   的 ranks 是 `0/8、1/9…`，不应凭编号假设物理邻近关系。
2. **MLA compact KV 是逻辑副本。** compact projection 使用
   `ReplicatedLinear`，当前每个 rank 写全部 token rows。真实量化、Graph、
   batch 配置下跨 rank 的数据等价性仍需实测。
3. **GQA 的 TP 重复是有条件的。** 只有拥有相同 KV head 的 replica ranks
   才能去重；拥有不同 head shard 的同 NUMA ranks 不能直接合并为同一份数据。
   当前正式 mempool 明确限定 BF16 MLA，GQA 需要额外模型路径与验收。
4. **Index K 不属于本次 BM 内存核算。** GLM-5.2 有 21 个 Index K producer
   层，但仍有 78 层独立 compact KV。Index K 保存在各 rank HBM，NUMA BM
   去重不会自动减少它的 HBM 或原生 PD 传输。
5. **共享数据不会自动消除读取流量。** 两个 D ranks 仍需各自的 selected KV，
   目前没有一次读出后组内分发的路径。16 份降为 8 份能使相同布局的 compact
   KV payload 和去重后的逻辑写入量减半；读取带宽收益需要测量。
6. **decode 当前依赖本 rank 的完整写入。** `runtime.py` 先写当前层 KV，
   再按本地 `_write_counts` 判断 prefix 可读。当前同层 write→fetch 之间没有
   TP collective；attention 之后的既有 TP 通信可用于后续读取的完成边界，
   但不能保护发生在它之前的本步新 KV 读取。这不等于必须新增 barrier。
7. **原有全 TP 生命周期可作为保守基础。** prompt ready、drain 和 release
   已在全 TP 上协调。共享后仍需保证每个物理 slot 的 owner/generation 一致，
   并等全部 readers/writers drain；同层读写次序需要另行证明。
8. **历史 NUMA 限制不能套到当前机器。** ticket09 的最新补记记录用户反馈
   BIOS 更新后 NUMA 问题已解决。旧奇数节点无 Fabric 窗口交集的快照只适用于
   旧环境，当前共享访问和并发带宽尚无实测依据。

代码入口：
[manager](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py)、
[layout](../../python/sglang/srt/hardware_backend/npu/mempool/layout.py)、
[runtime](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py)、
[offload](../../python/sglang/srt/hardware_backend/npu/mempool/offload.py)、
[config](../../python/sglang/srt/hardware_backend/npu/mempool/config.py)、
[TP linear](../../python/sglang/srt/layers/linear.py)、
[MLA model](../../python/sglang/srt/models/deepseek_v2.py)、
[TP control tick](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py)。
历史依据：[GLM-5.2 布局](../ascend-mempool/issues/10-glm52-indexer-sharing.md)、
[NUMA 最新补记](../ascend-mempool/issues/09-numa-allocation-followup.md)。

## 首个实现候选，待硬件验证

每个共享组在每一侧保存一份完整 compact KV replica，组内 ranks 都能读取，
每个 KV 写入单元只由一个 writer 写入。分开表达物理存储位置、BM participant、
writer 分工和 reader 集合。

按已确认的 P/D 对称分组，一个组可考虑包含 `P_a/P_b/D_a/D_b` 四个 BM participants，
各侧各存一份 replica；每个进程仍需自己的 handle 和设备映射。8 个共享组不表示
只有 8 个 Python handles，也不表示 P/D 共用同一个物理 NUMA。

物理分配可比较“每侧一个 owner 分配、其他成员映射”和“同 NUMA 各成员贡献一部分、
组成一份逻辑 replica”。后一种只有确实不重复存储才满足目标。部署 SDK 对零贡献
participant、多进程映射、地址连续性和 Graph 访问的支持需要最小硬件验证。
[MemFabric 官方 API](https://github.com/Ascend/memfabric_hybrid/blob/master/doc/pythonAPI.md)
提供 `peer_rank_ptr` 和 `gva_to_va`，将 GVA 转换为当前进程可访问的 VA；
这些接口本身不构成上述四 participant 共享方案已通过的证据。

2026-10-12 源码调查支持先试“每侧一个 owner 分配、其他成员零贡献”：
本地 MF 参考 checkout `29fbd1ae` 和旧版 `004d9317` 都允许 `localDRAMSize=0`、
`maxDramSize>0`；memType 按 max size 推导，VMM 为零贡献创建空 slice，
导出后在 import 时跳过零长度 slice，同时继续映射非零 owner 的贡献。
依据为两版本 `src/smem/csrc/smem_bm/smem_bm.cpp` 和
`src/hybm/csrc/mm/hybm_vmm_based_segment.cpp`。这是源码可行性证据，
不是部署版本四 participant 运行结果。应实测 follower 对 owner DRAM 的直接
kernel 读写、稳定设备映射、Graph 和并发带宽。物理 allocation owner
不承担其他 ranks 的数据转发。

具体未决点：新版 `hybm_vmm_based_segment.cpp:804` 的 SDMA reach 查询依赖
实际映射记录，零贡献成员没有映射；`smem_bm_entry.cpp:922` 可能据此报告 IDLE，
组状态重建会消费该状态。正常 ACK 的 CONNECTED 路径仍存在，因此不能仅凭该
源码分支断言初始化失败。旧版 reach 查询直接返回 true。最小实验需同时检查
正常 join、查询／状态重建和 drain/关闭；重连只验证 SDK 成员状态，不据此扩展
服务的故障恢复合同。历史部署 commit `c01f3ad…` 不在这两份本地对象库内，
上述版本均不能代替实机 SDK 构建身份。

首个实验布局为四个 BM participants，物理贡献为 `[S_P, 0, S_D, 0]`，统一
max stride；读写使用各侧非零 owner 的映射。分别验证分工写入、不重叠且完整的
KV、两个 rank 并发读取、Graph、Q5 的已有 TP 通信次序，以及实际 NUMA/物理
DRAM 占用。映射的虚拟空间大小不作为物理 replica 数的证据。

| 写入分工候选 | 对当前路径的影响 | 性能问题 |
| --- | --- | --- |
| token/block | 保留完整 row copy，需要证明组内逻辑 prefix 完整 | 单请求 decode 每步只有一个新 token，当步可能只有一个 writer |
| layer | 保留完整 row；78 层可分为各 39 层 | 每层单 writer，另一 rank 的读取依赖其完成 |
| feature | 单 token 可由两个 ranks 各写半行，需要改 copy/layout | 小拷贝与等待两半完成可能抵消带宽收益 |

这些是待比较的方案，不预设每层增加一次 barrier。

## 复用已有 TP 通信的同步方案

GLM DSA 当前 decode 顺序为：

```text
本层 Q/K、Index K 与 top-k 计算（或复用已有 top-k）
  → mempool.write_layer
  → sparse cache lookup / BM miss fetch
  → attention / o_proj
  → prepare_mlp 中已有的 attention TP 通信
  → MLP / 后续层 / 后续 decode step
```

`forward_dsa_prepare_npu` 中的可选 all-gather 和 indexer 位于 mempool 写入之前；
不能用这些先发生的通信证明后来发生的 KV 写入已完成。
当前交付脚本的 DP1、dense TP1、DeepEP 配置在 `prepare_mlp` 中使用 attention
TP reduce-scatter；其他受支持模式可能使用 all-reduce，需要按实际模式验证。
最直接的代码位置是 `ascend_backend.py:2819`、
`deepseek_v2_attention_mla_npu.py:496,538,575`、`deepseek_v2.py:2686`、
`communicator.py:1335`。

已有 TP 通信有实际的设备 stream 依赖。
[官方 ProcessGroupHCCL 源码](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/distributed/ProcessGroupHCCL.cpp)
的 `syncStreams` 在 current stream 记录 event，使 HCCL stream 等待前序工作；
`WorkHCCL` 的等待路径将通信完成依赖接回 current stream。
它支持复用既有通信的判断；部署版本、Graph capture/replay 和 BM 跨 NPU
可见性仍需对应验证。CPU 调用返回不应被当作设备全部完成。

**首版完成次序（Q5，按并行要求更新）：**

1. 指定 writer 写入共享 mempool 的所属部分。同 producer stream 保留作对照；
   独立 stream 在 compact KV ready 后尽早提交，争取覆盖 indexer 和 attention。
2. 每个 rank 直接用本地 `k/k_rope` 填充本步新 token 的 selected KV，
   将这一项排除出 BM miss；历史 token 延续本地 cache 命中／共享 BM 读取。
3. 同层 join 为对照；一层滞后候选在 attention(L+1)/o_proj 后，将 write(L)
   的具体完成事件接入已有 TP(L+1)，末层尾部收齐。只有该依赖覆盖共享组全部 writers、
   且 BM 写入已对 readers 可见，才将本步数据视为后续步骤可读的历史 KV。

该方向不新增独立跨 rank barrier，也不需要让写入跨 decode step 任意拖延。
本步 KV 已在各 rank 存在，首版优先直接消费它；不先引入一般化的 pending 队列。
当前代码尚未实现这条本地供给路径：newest token 若被选中，正常路径会 miss
后从 BM 读回。读取有效性／写入计数也需区分本地新 KV 与共享历史 KV。
独立 stream 的提前提交、current/hit/miss 互斥、buffer 生命周期和 Graph
fork/join 见[执行设计](execution-design.md)。current-fill 与 BM miss 独立提交，
只在 attention/refill 消费端汇合；两组 source/metadata 交替作为有界跨层
候选，复用前覆盖所有消费者。跨步延迟仍不纳入首版。

代码见 [decode backend](../../python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py)、
[NPU DSA module](../../python/sglang/srt/hardware_backend/npu/modules/deepseek_v2_attention_mla_npu.py)、
[layer communicator](../../python/sglang/srt/layers/communicator.py)、
[service 配置](../../ascend-mempool-test/scripts/run_service.sh)
和 [sparse cache](../../python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py)。

## 容量估算的口径

按已确认的 GLM-5.2 布局，单份 compact KV payload 每 token 为
`78 × 576 × 2 = 89,856 bytes`。
用户所说 3M 是每 NPU 的 Index K 容量。在当前 TP16/DP1 的 replicated Index K
语义下，它对应同一批逻辑 token，不乘 16 视作 48M 独立 token。
若要为 `3,000,000` 个逻辑 token 配齐全部 78 层 compact KV，
单份约 `251.05 GiB`；16 份约 `3.92 TiB`，8 份约 `1.96 TiB`。
这是配套 compact KV 的预算，不表示 Index K 占用这些 DRAM，也不是每机实测
占用或可服务容量：prompt KV 在 P，
decode KV 在 D，预算需按各侧实际 token 数拆分，并另计固定 slot 容量空洞、
对齐、其他进程内存和 HBM 资源。
现有 UniDexCopy 单 layer span 的 `UINT32_MAX` 限制也需单独检查。

## 后续决策与验证

业务目标、同步方向、P/D 分组、性能门槛和执行优先级均已确认。
2026-10-14用户接受TP1/2/4组件结果并关闭01。默认每NUMA两个ranks，按分配
结果可形成不同成员数的共享组；共享开关与NUMA分配独立。固定slot分配改造
和容量冲顶不纳入本轮。实施入口改为02真实模型接入与并行。

组件已证明部署环境下的共享读写并完成性能采集，原始物理NUMA页证据仍缺失。
后续必须验证生产manager接入、真实模型KV等价、Graph发布与释放，以及端到端
吞吐和ITL。组件数据不能代替服务验收，用户对01的接受也不提前关闭02/03。

后续依赖：明确物理分组与数据等价关系 → 共享映射可行性 → writer/publication
合同 → Graph 与生命周期合同 → 同负载性能门槛与容量验收。
NPU 验证继续由用户执行；未来入口遵循仓库的一键 setup/readiness/requests/
checks/reports/cleanup 要求，日志保存在 `/home/cryang/log/`。
