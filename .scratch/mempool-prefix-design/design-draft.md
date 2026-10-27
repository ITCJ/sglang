# Mempool 前缀复用：初步设计草案

**Type:** design discussion
**Status:** needs-info
**State:** open

日期：2026-10-17。代码基点：`cryang/dev/mempool` / `d328af12bd`。
本文记录本轮已确认方向、源码证据与待核对建议；不是已完成实现，也不替代旧票据的验收记录。
术语见根目录 [CONTEXT.md](../../CONTEXT.md)。

## 1. 当前进展

| 范围 | 已有能力与证据 | 当前边界 |
|---|---|---|
| 正式 mempool 数据路径 | 原 01、02、03、10 已由用户接受关闭；P 保留 native HBM、写 P DRAM，D 写 D DRAM并稀疏读取两侧 KV | 原 04–08 的独立验收要求仍存在 |
| Index K | GLM-5.2 的 21 个 producer 与 78 层 compact KV 已分别管理；native handoff 保留 Index K | compact KV 的共享不会自动共享 Index K |
| NUMA 组件 | 共享 01 已接受；固定形状 TP1/2/4 的读写结果已归档 | 物理页 NUMA 落点原始标记仍是 unverified；组件结果不等于真实服务验收 |
| 最新提交 | `d328af12bd` 实现 NUMA 分配、组成员、owner、身份及端口的纯 CPU sharing plan | 真实服务仍拒绝 `share=1` / 显式 map，提示后续 ticket02-2；共享 02 未完成 |
| 此轮设计 | alive / prefix、全局目录、page 管理、跨请求复制 | 属于新增设计，当前固定 slot 实现尚无该完整路径 |

旧会话入口有“共享 02 尚未实现”的快照；应以最新提交和
[02-1 交付记录](../ascend-mempool-numa-sharing/ticket-02-1-summary.md)为准：配置规划已实现，数据路径尚待接入。
当前分支比本地 `origin/cryang/dev/mempool` 多 1 个提交；工作区还包含原有未提交设计文档。
本轮只阅读历史验收证据，没有重跑 CPU suite 或硬件测试。

当前代码的实际地址逻辑是 `slot * token_capacity + token_position`，
见 [layout.py](../../python/sglang/srt/hardware_backend/npu/mempool/layout.py) 的 `KVLayout.row_index`。
[manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) 的
`initialize_rank_pair` 仍要求 16 个物理 slot 和两成员 BM session。
改为 page 管理需要改请求映射、分配、读取 metadata 与释放条件，不能仅替换 router 的索引。

## 2. 本轮确认的设计

| 项目 | 当前约定 |
|---|---|
| 两个逻辑 pool | alive mem pool 保存请求期间的 KV；prefix mem pool 保存跨请求复用的 KV。alive 内部按位置/请求逻辑分区，可以有物理 replica；两类之间不做物理去重 |
| 一致性哈希 | 仅用于 prefix mem pool 的节点放置；alive 默认按生产 KV 的计算实例及本地 NUMA 配置 |
| alive 访问 | 按 page 分配和回收，decode 按 token 选择并经 UniDexCopy 取入计算设备；用满可用本地 NUMA，不为 DP attention 新增额外负载均衡 |
| prefix 访问 | 按 page 查找和传输；prefix 节点中的独立进程执行复制，router 发任务并协调完成 |
| 传输实现 | 恢复 prefix 到 P HBM、alive 完成后迁入 prefix 均纳入 demo；连续 SDMA 与消耗 vector core 的 MTE 都保留为候选，尚未定唯一机制 |
| 统一目录 | 候选为一个逻辑 mempool 目录，包含 prefix 和 alive 中可复用的 prefill KV；活跃 decode 新 KV 不加入可共享前缀目录 |
| 请求间复用 | B 命中 A 的 alive prefill 时，复制为 B 自己拥有的数据；demo 不让 B 长期引用 A 的 alive pages |
| prefill 计算 | 复用的 prefix KV 必须恢复到 P_B 的 HBM；chunk prefill 同样遵守此条件 |
| B 的 alive DRAM | 主要为 B 后续 decode 的 sparse 访问提供 KV 来源；命中 prompt 默认放入 P_B 的 DRAM。可在请求开始时选择 D 侧作为 prompt 的目标存储，具体优化尚未落地 |
| 命中段与新算段 | 缓存传输进程复制命中段到所选 alive 目标；P_B 只向该目标写新计算的 KV |
| Router / allocator | router 选择 P/D 和存储位置、预留容量、维护共享目录与传输任务；具体页由相关 TP ranks / 存储 owner 协调分配，不由 router 维护空闲页表 |
| KV group | 请求下发后建立跨 P/D attention 分组及存储位置的逻辑关联；设备预先可访问的候选位置可以大于本次 group |
| Replica 索引 | 同一 attention 分组所需的同内容 NUMA 副本保持一致的 page 索引与 layout；保留逻辑身份到存储位置的映射。不同 DP 请求、P/D 不同内容段无需使用同一页号 |
| P/D 并行度 | demo 支持 P/D attention TP 宽度不同；prompt 副本先沿用 P 侧布局，按 D reader 拓扑扩展副本留待优化 |
| P/D 分散存储 | P 生成的 KV 默认留 P，D 生成的 KV 默认留 D；sparse 读取通过位置映射选择源，不要求整理成连续的请求存储 |
| Prefix backup | 接受从分散源异步直拷到 prefix 目标偏移，让目标页在复制时成形；以不明显损害 backup 性能为条件，不新增独立的 alive 整理步骤 |
| 请求完成 | 最终释放该请求的 alive 数据；按缓存准入策略选择是否保留到 prefix，并把已物化新 KV 对应的 hash 和可用位置告知 router |
| Index K | 用户已确认将 prefill 的 Index K 与必要 scale 作为 alive 附属数据保存在 DRAM，供跨请求复用和 backup；D 的计算仍使用所需设备侧状态 |
| 待讨论 | 容量上报与副本协调分配的具体合同、Index K 附属 buffer 的布局/副本数/写入时机、prefix backup 的 layout / 分段开销 |
| 流水候选 | 参考 GPU 主线的按层恢复：当前层所需 prefix 数据就绪即可开始该层，后续层继续复制；需先理解真实实现 |

这里的 P_B / D_B 表示为请求 B 选择的 prefill / decode 实例，B 本身不是机器名。
“一份逻辑 mempool”暂不推导为单个物理 allocation、全局连续地址或单个进程。
下文 P_B DRAM 的流程图描述默认放置；若请求开始时选定 D 侧保存 prompt，
缓存进程和 P 的两条 DRAM 写入路径均指向该目标，P HBM 的计算路径不变。

## 3. 两个恢复目标与计算重叠

设 prompt 长度为 L，实际可复用、已经对齐且状态完整的前缀长度为 H。
源可以是 prefix mem pool，或另一个请求已发布的 alive prefill pages。

```text
缓存源 [0,H) ──优先恢复──> P_B HBM [0,H)
                              │ 对应层 ready
                              ▼
                       P_B prefill [H,L)
                              │ 只写新生成 KV
                              ▼
缓存源 [0,H) ──后台复制──> P_B alive DRAM
                            [0,H) + [H,L)
                              │ 整体 ready 后供 D_B 稀疏读取
                              ▼
                       D_B 的 selected KV / attention
```

这是两个不同目标：HBM 副本服务 prefill 计算；alive DRAM 副本服务后续 sparse decode。
不把“已复制到 B 的 alive”当成“可以直接在 DRAM 上高效执行 prefill”。
也不要求 P_B 为命中段再做一次 HBM→DRAM 回写；命中段由缓存源复制。

### 按层流水的候选合同

整段恢复可以作为最初对照。按层流水候选中，对每个 layer l：

1. 分配 P_B 所需的 HBM 页，安装 token→page 映射。
2. 缓存传输完成本层需要的命中 KV 及必要附属状态。
3. 计算 stream 等本层完成事件，执行本层未命中 token 的 prefill。
4. 后续层的 prefix 恢复继续推进；P_B DRAM 的后台填充独立推进。

同一层的 suffix attention 仍需要其全部必需的历史 prefix KV；“按层流水”不能解释为
该层只收到任意几个 page 就能开始计算，也不是 chunked prefill 的同义词。
完整缓存到 GPU 之前能否从 L3 跨 L2 进一步流水，是待下一位 agent 核实的主线行为。

当前 checkout 已找到局部机制的直接证据：
[cache_controller.py](../../python/sglang/srt/managers/cache_controller.py) 的
`start_loading` 提交 host→device 传输，给每层完成回调注册 event；
`LayerDoneCounter.wait_until` 让当前计算 stream 等待对应层。
[memory_pool.py](../../python/sglang/srt/mem_cache/memory_pool.py) 的
`MLATokenToKVPool.get_key_buffer/get_value_buffer` 在读取该层 buffer 前执行这个等待。
完整 GPU 调用链、Graph 和不同 HiCache 模式仍交由新仓库按实际版本展开。

概念性就绪条件：

```text
prefill_layer_can_run(l) = restored_hbm_prefix_ready(l, required_state)
                          AND preceding_model_dependencies(l)

prompt_dram_ready = copied_hit_prefix_ready AND computed_suffix_writes_ready
decode_can_start = prompt_dram_ready AND index_k_ready AND handoff_ready
                   AND request_bindings_valid
```

上述 ready 均要求涉及的 shards / replicas / ranks 达成所需完成条件。
`prompt_dram_ready` 不必阻塞 prefill，但必须阻塞依赖其完整内容的 decode。
HBM 恢复和后台 DRAM 填充会竞争源端带宽；建议优先保障即将计算的 HBM 层，测量 DRAM
后台传输对 TTFT 和 decode 准备时间的影响。具体复制队列策略尚未定案。

## 4. Router workflow 草案

| 阶段 | 动作 | 可继续推进的条件 |
|---|---|---|
| 1. 选择与预留 | 查找有效 prefix，按容量选择 P/D 与 alive 位置并预留；相关 owner 协调分配页，建立请求 KV 关联；临时 pin 缓存源 | 源身份和 generation 有效；所需副本、HBM 和 decode 资源足够 |
| 2. 恢复计算状态 | 向传输进程提交 source / destination / page ranges；优先恢复 P HBM | 整段或对应层恢复完成，取决于流水模式 |
| 3. 计算与填充 | P 计算未命中段，同时缓存进程填 P DRAM 的命中段，P 写 DRAM 的新算段 | 两个 writer 的逻辑 token 范围互斥；buffer 保活 |
| 4. 交接 | 传输 Index K 和 native handoff 状态；确认完整 prompt DRAM 可读 | D 同时满足 prompt / Index K / handoff 的就绪条件 |
| 5. decode | D 按 token 读取 alive 中的历史 KV，把新产生 KV 写到自己的 alive；router 不收逐 token 的共享目录更新 | 本步写入和历史读取满足各自依赖 |
| 6. 完成与保留 | 停止新引用，drain，按准入策略复制到 prefix；提交 hash、位置和可恢复状态，再回收 alive | 所有计算和复制均不再访问待回收页 |

第 4 步可以与第 3 步中无依赖的部分重叠；不把文档的编号当成必须串行的设备执行顺序。
第 6 步中的“从 alive 删除”表示安全回收后的最终状态；生成最后一个 token 不等于 drain。

建议 demo 将完成路径分为：

```text
完成请求 → 撤下 alive 的新复用入口 → 等已有读取/复制结束
         → 若准入：写 prefix 并确认完整 → 发布 prefix 的可用位置
         → 释放 alive pages
```

复制尚未完成时，目标 prefix 只能是 staging，不得提前用于命中。
若不准入或缓存写入失败，停止引用并 drain 后释放 alive；缓存写失败本身不应推翻已成功的生成。
已有 B 的源复制任务仍会临时 pin A，A 最终回收需要等待这些复制；B 的整个 decode 则不依赖 A。

## 5. 逻辑索引、page 与 KV group

### 已确认的索引边界

用户确认：一个逻辑 alive pool 可由多处物理区域构成；同内容 replica 保持一致页索引，
同时保留逻辑身份到位置的映射。不同 DP 请求、不同 P/D 内容段、不同生命周期 pool 不强制统一物理页号。

以下名称区分同一个“index”可能指向的对象：

| 名称 | 含义 | 是否跨 rank 相同 |
|---|---|---|
| token position | 请求序列中的位置 | 对同一请求的同一段计算具有相同语义 |
| logical page ID | 一个 namespace 内的一段 KV 的逻辑身份 | 同内容需具有一致含义；具体编码尚未定案 |
| allocator page index | 某个物理存储区域中分配的页号，不是操作系统物理地址 | 同内容 replica 采用一致页索引；其他资源域独立 |
| device address | 某进程/设备访问该页的地址 | 不能以数值相同为前提 |
| prefix hash | 含前序上下文的内容标识 | 相同兼容 KV 才可相同，不表示数据已经可用 |
| Index K | 稀疏 indexer 选择 token 所需的模型状态 | 是数据，不是上述目录索引或 hash |

若以完整请求起点对齐逻辑页，token t 的逻辑页为 `t // B`，页内偏移为 `t % B`。
这不要求 P/D 的物理分配也拼成该页：映射可先确定数据段及其 replica，再查 page index 和有效偏移。
decode 按 token 选取，分配/回收以整页执行。D 段以自身起点还是请求绝对位置对齐，仍待定。
连续逻辑 token 不保证物理页连续；复制按源和目标实际相邻范围合并。

### TP / DP attention 与副本例子

这里用户的“TP2/DP4”“TP4/DP2”指 attention TP 宽度和 attention DP 数。
按当前 `derive_attention_widths/ranks`，两例在 CP=1 时均对应总 TP=8。

| 计算配置 | 一个 attention DP 分组 | 该组同内容 KV 的物理副本 | 一致性要求 |
|---|---|---|---|
| attention TP=2 / DP=4 | ranks 0,1 | 一个 NUMA 上一份 | 两个 rank 使用同一页映射 |
| attention TP=4 / DP=2 | ranks 0,1,2,3 | 两个 NUMA 上两份，各供两个 reader | 两份的页索引、页内 layout 一致；base address 可以不同 |

以上适用于当前同逻辑 compact KV replica；不能把不同 head shard 当作相同字节副本。
比如 A 的一页在两份 replica 中均为 index 37，rank 0/1 选择 NUMA0 的 base，
rank 2/3 选择 NUMA1 的 base。另一 DP 请求 B 的 local index 37 可表示另一份数据，需以资源域区分。

同索引便利复制描述与映射复用，但不是字节直拷的必要条件：相同 layout 的页即使页号不同，
也可以复制到不同目标偏移。真正需要保持的是元素布局、stride、有效范围和正确的目标位置。
若要求同内容 replica 的 allocator index 一致，就不能让它们各自独立分配/回收后仅靠广播页号补救。
建议由该 replica set 的 owner 协调逻辑页槽和各处的保留/回收；具体协议未定。

### 动态 KV group 与可访问范围

同一个 D attention 分组可接收不同 P 发来的请求；请求下发后才建立本次 P/D 和 KV 位置关联。
这不要求每个请求重建底层连接，也不要求只映射当前请求的源：目标运行时应能访问所有候选位置，
请求绑定从中选择实际存储。它与既有同主机/同 NUMA 的 `Mempool sharing group` 是不同概念。

当前代码仍是固定 P/D 配对的两成员池，不代表已经实现全部候选位置的连接、地址表及动态绑定。
候选位置的映射准备与请求 KV ready 分开；新增位置如何保持 Graph 地址与 metadata 稳定需另定实现。

用户确认 demo 支持 P/D attention TP 宽度不同，prompt replica 先沿用 P 侧布局。
例如 P 宽度 2、D 宽度 4，D 的四个 reader 可能访问 P 同一 NUMA 副本，超过原来的两 reader 性能预算。
这是已接受方向带来的已知性能例外，不能假定它已享有两副本的读带宽；后续再按 D reader 拓扑优化。

### Router 容量与 owner 分配

已确认 router 负责位置选择和容量预留，不维护各池的物理空闲页表。
不把命中 prefix 源的快慢作为当前 P/D 分配依据；支持在入场时根据 D 侧余量等因素，
选择把 prompt 放在 D 附近。该优化关注后续 sparse 读取，不要求增加 DP attention 请求重平衡。

建议 owner 上报扣除 replica 成本、已预留及尚未 drain 占用后的可分配逻辑容量。
对每份逻辑页都必须有两副本的固定资源组合，两侧可用页为 100 / 60 时，最多新增 60 逻辑页，
不是 160；若页号也必须相同且空闲集合已分歧，真实上限是共同空闲页号数，可能小于 60。
容量对应的物理资源先于请求存在；router 创建请求 KV group 时绑定并预留这些资源，
不能为多个重叠 group 重复记账同一份空闲空间。容量报告和原子预留的具体接口待定。

### 分散 alive 与 prefix backup

用户确认 sparse 访问不要求把一个请求的 P/D KV 拼成连续存储。
每页 16 token、prompt 长 30 的示例：P 保留 16+14，D 保留随后生成的 KV，
alive 读取根据 token 位置选择正确的源地址及偏移。

用户接受的 backup 方向是：按目标 prefix 布局直接提交异步字节复制，让目标在复制时成形。
例如目标第二页可由 `P 的 14 token → dst offset 0` 和 `D 的 2 token → dst offset 14` 填充。
不先整理 alive，不增加独立中间拼接 buffer。全部有效范围及所需附属数据完成后才发布目标页。
是否不明显影响 backup 性能，需测量任务段数、提交开销、吞吐与完成延迟，当前不承诺零开销。

需继续核对：

- 源/目标的 dtype 和页内元素布局相同才可直接字节复制；layer-major 与 page-major 的外层布局差异可能增加分段。
- D 从本地 offset 0 开始时，prompt 尾部错位可能使后续多个 prefix 页跨两片源页，不只影响边界页。
  可比较跨连续目标合并复制、D 首个页保留对齐空洞等方法；后者会消耗额外容量，尚未选择。
- 若一致性哈希逐页分散目标，连续合并机会会变少；因此 hash 放置单位与 backup layout 需要一起决定。
- Index K / scale 等附属数据需有自己的源范围与完成条件；只复制 compact KV 不能发布完整命中。

## 6. NUMA 约束的历史依据

已查本地 Codex 会话，2026-10-14 用户明确决定默认一个 NUMA 对应两个 TP rank，并保留配置能力。
会话：`rollout-2026-10-12T16-16-48-01a12f2f-8df2-7aa1-ac92-993f870ff756.jsonl`，
用户消息时间 `2026-10-14T06:22:40.408Z`（本地 14:22），原文为：

> 根据我的判断我认为目前默认1numa node对应2tprank即可

对应 [固定形状报告](../ascend-mempool-numa-sharing/visuals/cd-v1-resume2/report.html)
使用 Graph / block_dim=48，每 rank 读取 top-k=2048、batch=16，消费者分别在两侧计时。

| 配置 | 本机读取组 P50（P / D，ms） | 另一主机读取组 P50（P / D，ms） |
|---|---|---|
| 2-S0：两个 rank 集中共享 | 0.6926 / 0.6931 | 0.8586 / 0.8624 |
| 4-S0：四个 rank 集中共享 | 1.3377 / 1.3381 | 1.3434 / 1.3438 |
| 4-ID：四个 rank 分散独立池 | 0.4310 / 0.4306 | 0.8499 / 0.8508 |

这支持避免把更多 reader 集中到单个 NUMA。共享减少物理副本与重复写入，不会自动减少每 rank 的读取量。
用户在本轮将“每 NUMA 最多两个持续 sparse reader”作为当前部署约束；这是该平台/负载下的工程选择，
不是由这组实验证明的通用硬件极限。原报告的 NUMA 物理页落点证据限制继续保留。
后续讨论接受 P/D attention TP 不同时先沿用 P 侧 replica，这是上述性能规划的明确例外，见第 5 节。

影响：alive 按本地 NUMA 和 reader 预算配置；B 使用自己的存储，避免长期叠加 A 的 reader 压力。
prefix 的一致性哈希只均衡放置，不能自动均衡热门 prefix 的读取。
后台复制也消耗源 NUMA 带宽，需要传输任务准入；这不等于新增 DP attention 的请求重平衡。

用户补充的拓扑性能依据：sparse 小块读取跨越更多 UB switch 时下降明显，prefix 恢复和 replica
等大块复制的差异较小。此处记录为用户的部署经验；本轮未新增不同 hop 数的测量。
因此入口放置优化可以优先降低 D 的长期 sparse 路径开销，不必优先追逐最快的 prefix 源。

## 7. 一致性哈希与目录：建议补齐的合同

一致性哈希决定候选节点，不保证命中、可读或绝对容量均衡。
等长 page 加权放置可以改善容量分布；热点读取、副本策略、成员变更和节点满额仍需单独处理。
建议 demo 先固定成员配置，后续再实现在线迁移；这是建议，尚未成为约定。

建议使用链式内容标识：`h_i = H(namespace, h_(i-1), page_tokens_i)`。
namespace 表达模型/版本、adapter、KV layout、dtype、位置语义及必要隔离信息。
同样一页 token 出现在不同前文后，其 KV 通常不同；仅 hash 当前 page token 不够。
逻辑内容身份与具体 shard / replica 的物理定位应分别记录。

目录可用 `hash → 多个可用位置` 表示同内容存在于 alive 与 prefix；这不是两类 pool 的物理去重。
位置至少附带 pool 类别、page 范围、布局身份、generation、ready 状态和可恢复的附属数据集合。
存储 owner 验证并保护真实页；router 维护选择、容量预留、目录与任务信息，具体页由 owner / TP ranks 协调。
复制任务携带 request attempt / task ID / source generation，重复或迟到完成不得修改新 owner 的页。

建议最初只发布已完成的完整 page；尾页仍服务当前请求，但暂不对外作为完整 prefix 命中。
是否做更细的有效长度和部分页复用，留待主线机制讲解后决定。
alive prefill 什么时候发布也需核对：demo 可先在完整 prefill 就绪后发布，之后再评估逐 chunk 发布。
这些是建议，不是对用户“page 管理”要求的既定推论。

## 8. 原生 prefix cache：当前已核实的机制

### 数据与树

`ReqToTokenPool` 保存请求位置到 KV slot 的映射；KV pool 保存真实模型数据；allocator 分配/回收位置。
radix tree 保存 token 前缀、对应 KV 索引、拓扑与引用状态。
树节点可覆盖多个 token/page，节点拆分通常只是拆 metadata 和索引范围，不复制 KV payload。

示意：page_size=2，已有已物化序列 `[10,20,30,40,50,60]`。
新请求 `[10,20,30,40,70,80]` 命中前 4 个 token，树可拆成公共 `[10,20,30,40]` 加两条后缀。
原生 L1 可以让两个请求指向同一组受保护的 KV indices；当前 demo 的跨请求 alive 复用则创建 B 的副本。
这是一项存储/访问成本选择，不能从“都采用 radix tree”推出相同物理共享行为。

`inc_lock_ref/dec_lock_ref` 保护请求正在使用的路径；请求结束通常释放引用，缓存可继续保留。
缺空间时再按 eviction policy 淘汰可回收页。请求结束、树节点删除和物理页回收是不同动作。
未完成请求也可以在合适的计算完成边界插入可复用前缀，而非一律等生成结束。

### 当前入口与层级

当前 checkout 的 `mem_cache/registry.py` 默认创建 `UnifiedRadixCache`。
其 `match_prefix` 调用 tree core，再由组件确认可恢复状态；传统 `radix_cache.py`
适合解释基本算法，但不能把它当成所有模型当前的唯一默认路径。

官方 [HiCache 设计](https://docs.sglang.io/docs/advanced_features/hicache_design)把
L1 定义为实例内 GPU HBM、L2 为实例本地 host memory、L3 为外部后端。
Mooncake Store 是可选的分布式 L3；配置同一 namespace 才有对应跨实例复用范围。
alive/prefix 是生命周期分类，L1/L2/L3 是存储层级，二者不能一一等同。
官方 [Unified Radix Cache 说明](https://www.sglang.io/blog/unified-radix-cache)也展示了
按组件/附属数据判断复用边界的机制。

### Index K 的直接证据

- [memory_pool.py](../../python/sglang/srt/mem_cache/memory_pool.py)：`DSATokenToKVPool`
  创建 `IndexKeyCache`；数据与 KV 的 page/slot 身份关联。
- [index_key_cache.py](../../python/sglang/srt/mem_cache/index_key_cache.py)：
  K 和 scale 存在对应 page buffer，访问按层等待 transfer counter。
- [hybrid_pool_assembler.py](../../python/sglang/srt/mem_cache/hybrid_cache/hybrid_pool_assembler.py)：
  `_DsaStrategy.build` 构造 `KV + INDEXER`，`SidecarPoolSpec.indices_from_pool=PoolName.KV`。
- [mooncake_store.py](../../python/sglang/srt/mem_cache/storage/mooncake_store/mooncake_store.py)：
  生成 INDEXER 的 component keys；`batch_exists_v2` 综合 KV 与所需 sidecar 的可恢复边界。

用户已确认：prefill 的 Index K 与必要 scale 作为 alive 附属数据保存在 DRAM，供复用和 backup。
因此 A 的 prefill HBM 释放后，B 仍可从 A 的 alive DRAM 恢复完整前缀状态。
具体 producer layer 和布局按模型描述，不照抄层数；附属 buffer 的副本数与写入调度仍需细化。
共享目录发布前，主 KV 与必需附属数据都应达到对应范围的 ready 条件。
DRAM 备份可以与原生 Index K handoff 重叠，不能提前回收尚被复制使用的 HBM 源。

补充核对原生生命周期：P/D 交接完成时，正常 release 路径先把可缓存部分交给 radix cache，
再解除请求的引用；这不等于立刻把该前缀的 HBM 页还给 allocator。
在常规 HiCache L2 缓存模式下，DSA 使用主 KV host pool 与 `DSAIndexerPoolHost`，
以 `INDEXER` sidecar 保存 Index K / scale。请求 B 可从仍保留的 L1，或已完成备份的 L2/L3，
恢复完整状态；所有可用来源都缺少必要状态时应缩短命中并重算，普通 match 不自动向 D HBM 取回 Index K。

所以“P 已释放 HBM，D 独有 Index K”不能直接当成原生缓存的默认行为；新设计通过 alive DRAM 附属副本解决该来源缺口。
HiSparse 的 decode host KV 路径还需与 HiCache 区分：当前 `validate_hisparse` 要求关闭 radix cache，
不能据此宣称原生已经把活跃 host KV 发布成跨请求可复用前缀。
详见[源码核对](native-index-k-lifecycle.md)与[Index K 图解](native-index-k-explainer.html)。
该 DRAM 附属副本的方向已获用户确认，尚未实现。

HiSparse 的 prefix 分工补充：P 可以继续使用自身原生 prefix cache，HiSparse D 为每个请求分配 host KV。
当前 D 预分配路径明确要求 `prefix_len == 0`；P 的发送起点取 D 返回的 `decode_prefix_len`，
并从请求映射读取待传页。因此 P 命中 prefix 可以减少 prefill 计算，但在此 HiSparse 路径下，
D 仍需收到包含命中段在内的完整 prompt 主 KV；DSA Index K / scale 进入 D 的设备池。
D 的 sparse 热数据命中是同一请求的设备 buffer 复用，不是跨请求的 radix prefix 命中。

hash 更新也必须按实际 KV 写入长度生成：最后一个采样 token 可能尚未经过 forward，不能缓存不存在的 KV。

### Router 与 worker 的区别

当前 checkout 的 `experimental/sgl-router` 能使用 prefix signal、负载以及 KV events 形成 worker 候选。
事件带有 block hash、storage medium 和可选 DP 身份；不能据此断言 router 拥有每个 worker 的物理页 allocator。
worker scheduler 的 `Req.init_next_round_input` 执行真实 match；
`Scheduler._prefetch_kvcache` 发起存储预取，之后通过 cache/controller 推进恢复。
新设计把 mempool 传输任务协调加入 router，是需要另定合同的职责。
新仓库必须重新核实实际 gateway 版本、目录和可观测缓存层级，不能用当前分支路径代替主线证据。

## 9. 下一轮重点

1. 细化 replica 统一页索引的分配/回收与容量预留接口；避免重叠 KV group 重复记账。
2. 追踪 GPU 主线的 L2→HBM layer pipeline，区分它与 L3 prefetch、chunked prefill。
3. 细化已确认的 alive DRAM Index K 附属存储、复制就绪与保活期。
4. 核对 page 大小、D 段对齐、backup 目标布局和 hash 放置单位；再定尾页与准入策略。
5. 确定 router 与 allocator 的接口，以及冷热复制竞争时的队列预算。

交接分两步：先使用[独立的 GPU 主线框架讲解 prompt](sglang-main-agent-prompt.md)，
完成后再追加[包含完整设计背景的分析 prompt](sglang-main-design-followup-prompt.md)。
HiSparse 框架补充可单独追加[HiSparse HTML 完善 prompt](sglang-main-hisparse-followup-prompt.md)。
框架讲解与 HiSparse 补充继续使用中文 HTML。
完整设计追加 prompt 改为先对话讨论，每轮聚焦 2–3 个问题，再收敛 Markdown 草案；
当前不生成或修改设计 HTML，待用户提出整理页面时再推进。
图解：[overview.html](overview.html)。本草案不关闭或解锁任何原有 implementation ticket。
