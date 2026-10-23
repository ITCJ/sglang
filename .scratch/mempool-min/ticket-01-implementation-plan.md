---
title: Ticket01：Main Router / Sub Router 实施方案
subtitle: 真实冷请求、统一分配、显式释放与双机启动
template: doc
theme: blueprint
lang: zh
date: 2026-10-23
status: 实施提案；尚未实现
baseline: 3883624810 + 当前工作区
---

目标是让一个 Main Router 管请求全程，由两侧 Sub Router 执行真实资源操作。
Main Router 是独立 Python 进程。SR_P、SR_D 随推理服务加载。
每侧 TP rank 0 负责协调，每个 worker 保留自己的执行分片。

本文细化 [ticket01](issues/01-cold-request-lifecycle.md)。功能范围以 [spec](spec.md) 为准。
下文新增文件名、接口和命令均为拟定方案，当前不能按这些新命令启动。
现有代码事实与新增设计分别标明；本轮只形成方案。

本方案是 syncfile 所定义的 **sparse 阶段 0 的首个切片**。
私有副本和 alive 整槽分配是阶段 0 的临时后端约束。
整体目标继续包含 K1 共享及后续阶段，见[目标对齐核对](target-alignment.md)。
阶段 0 / L0 后接 K1，再推进阶段 1；四票结束不代表共享已完成。

## A 进程如何部署

```flow LR
客户端 -> MainRouter: token IDs / HTTP
MainRouter -> SR_P: 分配、绑定、释放
MainRouter -> SR_D: 分配、绑定、就绪、释放
SR_P -> P_workers: 有序 TP tick
SR_D -> D_workers: 有序 TP tick
P_workers -> D_workers: 原生 Index K / handoff
group P机器: MainRouter, SR_P, P_workers
group D机器: SR_D, D_workers
```

图中的 SR_P、SR_D 是逻辑角色。它们各自的协调者位于 rank 0 scheduler 内。
每个 rank 都有 SR 分片，包括 rank 0；分片与本 rank 的模型执行共进程。
Main Router 默认放 P 机，使用 CPU，不分配模型权重或 NPU KV。

| 进程或模块 | 数量 / 位置 | 负责什么 |
|---|---|---|
| Main Router | P 机独立进程 1 个 | HTTP 入口、注册、容量账本、请求编排、结果返回 |
| P 推理实例 | P 机，TP16 | 原生 HTTP / tokenizer / scheduler 和 prefill |
| SR_P 协调者 | P 的 rank 0 scheduler 内 | 分配 P alive、汇总 P 完成、确认释放 |
| SR_P 分片 | P 的每个 worker 内 | 本地映射、描述符校验、写回完成与原生清理 |
| D 推理实例 | D 机，TP16 | 原生请求接收、Index K 接收和 decode |
| SR_D 协调者 | D 的 rank 0 scheduler 内 | 分配 D 资源、安装两侧绑定、汇总 finished |
| SR_D 分片 | D 的每个 worker 内 | 解析本 rank 的 P/D 描述符、取数和 drain |

**拟定的三条通信路径**

| 路径 | 传输方式 | 内容与约束 |
|---|---|---|
| Main Router ↔ SR 协调者 | 新增 ZMQ ROUTER / DEALER | 结构化控制消息；只发小型元数据和 token IDs |
| Main Router → P/D 原生 HTTP | 复用 `/generate` | 创建原生 Req，保留 tokenizer / 输出返回路径 |
| P_i ↔ D_i | 复用 Ascend PD 连接与设备映射 | Index K、handoff、目标页索引；主 KV 从 mempool 读取 |

Main Router 只向每侧 rank 0 发控制消息。
网络线程拥有自己的 ZMQ socket，收发均通过有界队列。
它不调用 NPU，不修改 allocator，也不调用 TP collective。

HTTP 请求必须匹配 SR 已确认的 dispatch。
在进入原生 P/D 队列前检查 rid、room、输入和采样参数。
因此直接访问后端 HTTP 也不能绕过统一分配和串行门。

## B Main Router 的实现

拟新增 `mempool_router.py`，提供可启动的 Python 入口。
使用单进程、单事件循环；HTTP 和 SR 消息都交给同一请求控制逻辑。
拟采用仓库已使用的 aiohttp 与 pyzmq。
不把请求状态分散到多个 Web worker，也不引入外部数据库。

**对外接口**

| 接口 | 拟定行为 |
|---|---|
| `POST /generate` | 接收一个 token IDs 请求；首版使用 `stream=false` |
| `GET /health` | 仅表示 Router 进程存活 |
| `GET /ready` | 检查两侧注册、实际能力、TP 完整性及配对握手 |
| `GET /status` | 返回当前请求阶段、可用配额、待确认操作和故障原因 |

`/ready` 为 200 不代表当前空闲；第二个请求还要通过串行门。
首版忙时返回 429，不建立等待队列。
输入非法返回明确的 4xx；已知容量不足在分配前拒绝。
这些拒绝均不修改账本、目录和分配状态。

**五项核心职责**

| 职责 | 具体做法 |
|---|---|
| 注册与兼容检查 | 核对模型/布局身份、TP/DP/PP、rank 集合、页大小、池容量与会话 |
| 入口与 reserve | 校验输入后，一次预留四类资源；整槽计费 |
| 请求编排 | 分配 rid、room、op_id；依次驱动 alloc、dispatch、ready 和 free |
| 完成与账本 | 只根据实际完成回执更新资源；重复回执只生效一次 |
| 串行与 fail-stop | 上一请求全部归还后才接收下一请求；内部失败停止本轮 |

注册包括实际 `hbm_page_size`、S_P/S_D、槽数、Index K HBM 页容量和请求行容量。
还包括各类副本数、rank → NUMA、P 静态 prompt 上限和功能开关。
多 rank 的容量按共同可满足的配额计算，不能把副本容量相加。
ticket01 将 P alive Index K DRAM 和 prefix 标记为未启用，容量为零。

输入要求沿用规格：token IDs、greedy、单请求、单 chunk、B-token 对齐。
还要检查词表范围、实际上下文上限和双方采样配置。
拒绝不支持的文本、批量输入、logprob、LoRA、多模态和自定义生成方式。

**一次 reserve 的四项资源**

| 资源 | 记账单位 | 4096 输入 / 最多 128 输出时 |
|---|---|---|
| P alive | 一整个 S_P 槽 | 1 槽，额度为 8192 / B = 64 页 |
| D alive | 一整个 S_D 槽 | 1 槽，额度为 128 / B = 1 页 |
| D Index K HBM | 原生 HBM 页 | ceil((4096 + 128) / 128) = 33 页 |
| D 原生请求行 | 行 | 1 行 |

四项 reserve 在 Router 内作为一次状态变更完成。
Router 管额度，SR 管实际编号和分配结果。
Router 不再维护另一份可以自行挑选物理槽的 free list。
P 原生 HBM 用静态上限保证，不加入上述四项账本。

**保留的状态**

下面给出拟实现的记录形状，便于核对数据归属。
字段名是说明用的草案；序列化类型在实现时确定。
这些记录放在 Main 的 CPU 内存中，主 KV 字节留在 worker 管理的池中。

| 记录 | 索引与主要字段 | 何时改变 |
|---|---|---|
| 注册表 | 按实例会话索引；role、rank 集合、model/layout 身份、容量、能力开关 | 启动注册；身份不兼容则不 ready |
| 请求表 | 按 rid 索引；room、attempt、input_ids、max_new_tokens、phase、两侧页表、ready_tokens | 从接收请求保留到释放完成 |
| 操作表 | 按 op_id 索引；rid、kind、目标侧、预期回执、已完成标记 | 发命令时建立；实际 ack 只确认一次 |
| 资源账本 | 按机器、attention group、资源种类索引；total、free、outstanding | reserve 扣额度；实际归还后恢复 |
| 活跃目录 | 页哈希、parent_hash、位置列表、状态、owner hold | 分配后登记 writing；写完变 ready；回收后移除 |

账本每次转换都要满足 `total = free + outstanding`。
`outstanding` 表示尚未实际归还的全部预留。
alloc_ack 将预留关联到真实编号，不能再扣第二次额度。
同一侧 16 个副本共同服务一个请求，容量按全组共同余量计算。

**位置和页表怎么关联**

```text
Request[rid]
  identity: room + attempt + P/D session
  prompt_len / max_new_tokens / ready_tokens
  P_page_table: 按请求内逻辑页顺序排列
  D_page_table: 按生成段逻辑页顺序排列

PageEntry（示意字段）
  logical_page: 池内逻辑页编号
  token_start / token_count
  descriptors_by_kind:
    main_kv: [replica_0_handle, ..., replica_15_handle]

Directory[page_hash]
  parent_hash
  locations: [machine, pool, logical_page, state, owner_hold]
```

PageEntry 的位置可在分配后存在，此时数据仍可能是 writing。
只有 ready 才表示对应有效范围已经可读。
Main 发布位置时同时更新 ready_tokens，再通知 SR_D。
未写入的槽尾只占容量，不作为有效内容发布。

一个描述符列表代表同一逻辑页的多个物理副本。
相同 token 页出现在另一池时，会产生另一条位置记录。
目录跟踪内容与位置，页表按本请求的 token 顺序组织访问。

页哈希沿用现有命名空间和链式哈希语义。
首片的 `hit_len` 固定为零，仅跟踪当前请求的 alive 位置。
不创建 prefix 数据区；请求结束后按依赖删除活跃位置。
避免现在实现空的 load_back、fill 或 admit 执行器。

正常请求阶段为：

`allocating → dispatching → prefilling → decoding → finished → freeing → done`

任一内部阶段可进入 `failed`。
`finished` 不恢复 alive 容量；`done` 才打开下一请求的串行门。
首版暂存 D 的 HTTP 结果，完成释放后再返回成功响应。
HTTP 返回、P 完成以及最后一个输出 token，都不单独证明资源已归还。

## C Sub Router 的实现

拟新增 `mempool_subrouter.py`，由现有 `MempoolPDService` 接入。
保留 scheduler 已有的调用入口，避免再加入第二条独立调度循环。
SR 协调者负责决定本侧动作；分片负责执行与报告实际结果。

**mempool 里的真实数据是什么**

ticket01 的主 KV 使用现有 BM DRAM 池及 NPU 映射。
按 GLM-5.2 几何，一份逻辑数据可以写成以下形状。
这是地址布局说明；不会新建这样一个 CPU Tensor。

```text
P 主 KV：BF16 [78 层, 16 槽, 8192 token, 1 compact head, 576 维]
D 主 KV：BF16 [78 层, 16 槽,  128 token, 1 compact head, 576 维]

一个 rank 的主 KV 区域（layer-first）
  layer 0: slot 0 的 token ... | slot 1 的 token ... | ... | slot 15
  layer 1: slot 0 的 token ... | slot 1 的 token ... | ... | slot 15
  ...
  layer 77
```

每个 token、每层占 `576 × 2 = 1152` 字节。
每层内一个槽的 token 连续存储，跨层通过 layer stride 定位。
78 层有各自的主 KV；Indexer 共享 top-k 不会合并这些层。

| 数据种类 | 存放位置及形状 | ticket01 生命周期 |
|---|---|---|
| P prompt 主 KV | 每个 P rank 的 BM DRAM；上述 P 布局 | P 写回后保留到显式 free |
| D 生成主 KV | 每个 D rank 的 BM DRAM；上述 D 布局 | decode 逐步写入，drain 后等待 free |
| P 原生主 KV | 原生 HBM 的 512／64 两部分缓冲 | 写回、发送等依赖完成后可提前清理 |
| prompt Index K | P 原生 HBM，经既有数据通道到 D HBM | P 发送完成后可清理；D 保留到请求结束 |
| D 生成 Index K | D 原生 HBM；21 个 producer 层，每 token 128 维 BF16 | 与 prompt 共用本请求的预留索引范围 |
| P alive Index K DRAM | 本票未分配，能力未启用 | ticket02 接入 |
| prefix 主 KV／Index K | 本票未分配，能力未启用 | ticket02 接入保存，ticket03 接入命中 |

Index K 每层每 token 占 `128 × 2 = 256` 字节，没有独立 scale。
主 KV 供 sparse attention 取数；Index K 供 indexer 选择 token 位置。
两个结构的用途和就绪条件分别检查。

**B-token 逻辑页、mempool 槽和原生 HBM 页**

| 名称 | 含义 | 4096-token prompt 的例子 |
|---|---|---|
| DRAM 逻辑页 | 目录和页表的 B-token 单位 | prompt 有 32 个有效页 |
| P mempool 槽 | 为一个请求保留的 S_P 容量 | 整槽占 64 页额度；后 32 页尚未写入 |
| D mempool 槽 | 为生成段保留的 S_D 容量 | 整槽占 2 页额度；不再存一份 prompt |
| 原生 HBM 页 | 原生 allocator 的单位，本次为 128 token | 最多 128 输出时预留 33 页 |
| req_pool_idx | 原生请求行编号 | 用于索引 req_to_token；与 P/D 槽号独立 |
| Graph 行 | 图捕获的 batch 行，可能包含 padding | 16 行 Graph 仍只允许 1 个真实请求 |

P 槽 `s` 对应逻辑页 `[s × (S_P/B), (s+1) × (S_P/B) - 1]`，本例为 `[64s, 64s+63]`。
D 槽 `d` 对应逻辑页 `[2d, 2d + 1]`。
请求内页序号 `j` 映射到该槽起始页加 `j`。
这层页接口由整槽后端承载；本票不能把空闲槽尾分给另一请求。

假设本次 P 槽为 2、D 槽为 5、D 原生请求行为 7。
这三个编号分别来自各自的分配器，互相不要求相等。

```text
prompt_len = 4096
全局 token 位置 4095 -> P_i 的 slot 2，槽内位置 4095
全局 token 位置 4096 -> D_i 的 slot 5，槽内位置 0
全局 token 位置 4097 -> D_i 的 slot 5，槽内位置 1

D 槽内位置 = 全局 token 位置 - prompt_len
```

后两行只在对应 token 已经过 decode forward 时有有效 KV。
采样出一个输出 token，不表示该 token 的 KV 已经写入。
因此生成上限、预留容量和实际有效 KV 长度必须分开记录。

**每个 rank 持有哪些结构**

| 结构 | 关键字段或内容 | 负责的事实 |
|---|---|---|
| 现有 KVLayout / PoolLayout | layers、slots、tokens、heads、dim、dtype；P/D 布局与对齐 | 布局、边界及字节偏移 |
| 现有 MempoolKVManager / View | BM handle、P/D bases、层视图 | 实际映射和本进程可用的 NPU 地址 |
| 现有 RequestIdentity | room、attempt、p_session、d_session | 这是哪次请求尝试 |
| 现有 SlotLease | slot、generation、proof | 这是该槽的哪次占用 |
| 现有 MempoolPDControl | slot_owner、generation、请求记录 | 分片唯一的槽所有权事实 |
| 现有 KVRowBinding / Runtime | req_pool_idx、slot、prompt_slot、prompt_tokens；绑定张量 | 原生请求行如何访问两侧槽 |
| 现有写回记录 | submitted、completed、设备事件、KVWriteReceipt | 提交了多少、真正完成了多少 |
| 拟新增原生预留记录 | 请求身份、原生行、实际页／token 索引、消费进度、清理状态 | D 的原生资源属于谁、是否已归还 |

`SlotLease` 随槽复用推进 generation，旧租约不能操作新请求。
session 防止上一轮进程的消息干扰本轮。
attempt 隔离请求尝试；op_id 用于当前编排中的操作回执去重。
这些身份各管一层，不用一个 rid 替代所有校验。

现有 Runtime 保留稳定的设备绑定张量：
`row_slot`、`row_prompt_slot`、`row_prompt_len`、`row_decode_len`。
Graph replay 读取这些张量，绑定更新也需要正确的流依赖。
原生行 0 用于 padding，不能分配给真实请求；mempool 槽 0 则是合法槽。

**固定 16 槽会占多少 DRAM**

以下按上述几何和当前 PoolLayout 的 1 GiB 对齐计算。
BM 每侧贡献还包含 64 字节探针；列出的对齐值已计入探针。

| 主 KV 区域 | 每 rank 有效载荷 | 每 rank 对齐后 BM 贡献 | 16 个私有副本合计 |
|---|---|---|---|
| P：16 × 8192 token | 10.96875 GiB | 11 GiB | 176 GiB |
| D：16 × 128 token | 175.5 MiB | 1 GiB | 16 GiB |

这些是布局算术，未包含模型、原生 HBM、SDK 及其他运行开销。
启动仍须验证真实容量、NUMA 拓扑和映射可见性。
BM 固定区域在启动时建立；请求 alloc 取得已建区域的槽租约。
只有 1 个活跃请求，也仍按当前固定配置建 16 个槽。
两个 rank 落在同一 NUMA 节点，仍各持有自己的物理副本。

**每个 scheduler tick 的顺序**

1. rank 0 取出有界控制命令。
2. 在 TP CPU group 中分发相同命令。
3. 各 rank 采集原生与设备完成事实。
4. 汇总事实，校验同一请求和操作。
5. 按固定顺序执行获准动作。
6. 汇总实际执行结果。
7. rank 0 向 Main Router 发回执。

空闲时也推进 tick，避免控制消息无人处理。
任一 rank 失败或迟到，都不能被其他 rank 的成功覆盖。
准备阶段的校验成功不等于执行成功；ack 在执行结果汇总后发送。
设备操作提交返回也不等于完成，继续使用实际事件和 drain 证据。

**SR_P**

- alloc 时选择本池编号最小的空闲 alive 槽。
- 将同一个槽号安装到全部 P ranks。
- 返回逻辑页表，每页包含各 rank 的不透明描述符。
- dispatch 时核对本请求身份、页表与完整输入。
- Req 进入后，复用原生 prefill 和 mempool 写回。
- 汇总全部 rank 的主 KV 写回完成，报告 ready_tokens。
- 原生发送完成后，可清理 P HBM 和请求行。
- 保留 alive 租约及写回收据，直到显式 free。

**SR_D**

- alloc 时选择 D alive 槽，并预留原生请求行与全部 Index K HBM 页。
- dispatch 时安装 P/D 两张页表，逐 rank 校验对应关系。
- 本地分片将描述符解析为 runtime 所需的槽号和偏移。
- 等待 Router ready、Index K 完成及 handoff，才允许首 token 和 decode。
- 结束时停止提交，完成 drain，再清理原生资源。
- 全部 rank 清理完成后汇总 finished。
- 收到 free 后归还 D alive，汇总 free_ack。

**页描述符与所有权**

对外页表用逻辑位置及不透明描述符表示。
描述符内编码池会话、rank、数据种类、slot lease 和槽内页位置。
Router 只保存和转发，不从中计算设备地址。

只有持有映射的分片解析地址；D_i 读取映射中的 P_i 副本。
P 槽与 D 槽可以不同；原生 `req_pool_idx` 又是另一种编号。

以上 D_i 读 P_i 是阶段 0 的副本对应关系。
协议按数据种类保存描述符列表，不能把一 rank 一副本写成永久合同。
K1 将 alive 主 KV 改为共享组映射，其他三类 DRAM 数据各一份。
分片数量仍取决于 worker 数量；物理副本数独立注册。

现有 `MempoolPDControl` 的租约记录作为每个分片的唯一槽所有权事实。
SR 协调者从一致的所有权快照选择最小空闲槽。
新增受控操作使用显式 slot、rid 和租约身份，不再触发自主 ACQUIRE。
沿用已有 session、attempt、generation 校验，不增加故障恢复协议。

**D 原生资源如何做到足额预留**

这是需要新增的实质适配，不能只在 Router 账本上减 33。
拟新增 `mempool_native_reservation.py`，封装本请求的原生资源预留。

当前代码已有 `ReqToTokenPool.alloc_rows()`，允许在 Req 创建前取出原生请求行。
页分配器已有 `alloc(need_size)`，参数单位是 token，且必须按整页对齐。
预留 33 个 128-token 页时，应传入 `33 × 128 = 4224`。
返回值是实际 token 索引；各页的物理编号不保证连续。

SR_D 在 alloc 阶段调用这些接口，实际移走所需请求行和全部页。
任一 rank 分配失败则进入 fail-stop，不发送成功 alloc_ack。

| 时机 | 预留适配的行为 |
|---|---|
| alloc | 取出原生行和 ceil((prompt + max_new) / page_size) 页，保留实际索引 |
| 原生 Req 到达 | 将预留行绑定到 Req；原生 prealloc 使用已有的 prompt 索引 |
| 每次 decode | 从本请求预留范围取得下一个真实 token 的索引 |
| 原生内存检查 | 检查本请求剩余预留，不能再用全局空闲量重复收费 |
| 请求结束 | 原生清理归还已使用页；预留适配归还尚未使用的整页 |
| finished | 全部 rank 的行、已用页和未用页均实际归还后确认 |

预留范围与“已写入有效 KV 的长度”分开记录。
不能将 `kv_committed_len` 提前设为 prompt + max_new。
保留普通 decode 对 `kv_allocated_len` 的现有含义。
已转交原生生命周期的整页和未使用整页必须不相交。
实际尾页的未用部分随其所属整页归还，不能两边各 free 一次。

例如先向原生 prompt 路径提供前 4096 个预留索引。
decode 每处理一个新 token，再消费后续对应索引。
若提前 EOS，剩余索引中可能同时包含实际尾页的未用部分和未使用整页。
清理按整页归属划分，不能把同一页拆成两份各自归还。

该适配需要覆盖 prealloc、逐步取索引、容量检查和清理四处。
仅在新 Router 模式生效；不以改长序列计数器替代真正的预留。

## D 一个冷请求如何走完

**拟定消息契约**

请求消息携带版本、run_id、实例会话、rid、bootstrap_room 和操作身份。
需要回执的操作使用单调 op_id。
保留已有 request attempt 和 slot lease 身份，用于隔离迟到消息。
run_id 用于本次部署关联，不代表支持恢复旧进程状态。

| 消息 | 方向 | 成功语义 |
|---|---|---|
| register / register_ack | SR ↔ Main | 全部 rank 的真实能力被接受 |
| alloc / alloc_ack | Main ↔ SR_P、SR_D | 分片实际分配完成，返回页表和资源收据 |
| dispatch / dispatch_ack | Main ↔ SR_P、SR_D | 完整绑定和预期输入已安装；允许匹配的原生 Req 进入 |
| ready_tokens | SR_P → Main | 全部必要 rank 的 prompt 主 KV 已可读 |
| ready | Main → SR_D | Main 已发布该请求 prompt 的可读位置 |
| finished | SR_D → Main | 全部读写结束，D 原生资源已实际清理 |
| free / free_ack | Main ↔ SR_P、SR_D | 对应 alive 租约已实际归还 |
| error | 任一方 → Main | 冻结本轮，不再分配或发起新的 free |

`alloc_ack` 不表示可以计算；`dispatch_ack` 不表示 prompt 可读。
两个 SR 都确认 dispatch 后，Main 才向原生 HTTP 发请求。
HTTP 中的 rid、room、token IDs 和参数必须与 dispatch 一致。
该步骤复用原生 Req 创建、bootstrap 和响应返回路径。

P 原生队列会把 max_new_tokens 改为 1，用于自身内存估算。
输入一致性检查在这次改写之前完成。
SR 保留 Router 声明的生成上限，供 D 预留和跨侧绑定校验。

```sequence num
participants: Client, Main, SR_P, SR_D
Client -> Main: token IDs，最多生成 128
Main -> Main: 校验，关闭串行门，预留四类资源
Main -> SR_P: alloc P alive
Main -> SR_D: alloc D alive、原生行、Index K 页
SR_P --> Main: alloc_ack + P 页表
SR_D --> Main: alloc_ack + D 页表
Main -> SR_P: dispatch 输入与 P 绑定
Main -> SR_D: dispatch 输入与 P/D 绑定
SR_P --> Main: dispatch_ack
SR_D --> Main: dispatch_ack
note Main, SR_D: Main 并发调用两侧原生 HTTP，创建匹配的 Req
note SR_P, SR_D: 原生 bootstrap 与 Index K / handoff 传输继续沿用
SR_P --> Main: 所有 rank 的 prompt ready_tokens
Main -> SR_D: ready
note SR_D: 三个条件齐备后，输出首 token 并 decode
note SR_P: 原生 HBM 可提前释放，alive 继续保留
SR_D --> Main: drain 和原生清理完成，finished
Main -> SR_P: free P alive
Main -> SR_D: free D alive
SR_P --> Main: 实际 free_ack
SR_D --> Main: 实际 free_ack
Main -> Main: 核对账本，打开串行门
Main --> Client: 完整输出 token IDs
```

**D 的三个开始条件**

`Router ready AND 全部 rank Index K 完成 AND 全部 rank handoff 到达`

现有 transfer 成功与 `transfer_complete()` hook 可以继续承担原生条件。
新的 Router ready 替代旧 KV_READY 对开始 decode 的授权。
首 token 的 `_commit_transfer_to_req` 也必须位于此门控之后。

**释放所需证据**

P 的 free 还要确认自身写回、原生发送和本地原生资源清理已完成。
D finished 是远端消费者结束的证据，不替代 P 本地条件。
free 可先排队，但 free_ack 必须等条件全部成立。
下一请求等待两侧 free_ack；不能只看 D HTTP 返回。

提前 EOS 仍走完整清理，归还未使用的预留页。
生成上限为 1 时可能没有 D decode forward，也仍要正确清理。
硬件主验收要实际发生 decode 和 mempool 读取。

重复 ack 只确认一次；内容冲突的同号消息触发失败。
旧会话、旧 attempt 或旧 lease 的消息不能改变当前所有权。
客户端断开不等于 drain，不能据此提前 free。
主进程或 worker 故障后由运行入口协调终止本轮，不在进程内重试复用。

## E 怎样接入现有代码

当前已有写回、两侧取数、Graph、bootstrap 和原生传输。
新工作集中在控制编排、原生预留、入口门控及显式释放。

| 拟新增文件 | 承担的实现 |
|---|---|
| `python/sglang/srt/disaggregation/ascend/mempool_router.py` | HTTP / ZMQ 入口、Main Router 请求状态与账本 |
| `python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py` | 注册、命令、回执、逻辑页表的类型与校验 |
| `python/sglang/srt/disaggregation/ascend/mempool_subrouter.py` | 协调者、rank 分片和同侧完成汇总 |
| `python/sglang/srt/disaggregation/ascend/mempool_native_reservation.py` | D 请求行、Index K 页的真实预留与消费 |
| `ascend-mempool-test/scripts/run_mempool_min.sh` / `.py` | 双机自运行启动、验收、收集与进程清理 |
| `ascend-mempool-test/configs/mempool-min.example.json` | 部署配置模板；实际环境必须核对 |

这些文件名是拟定落点，可随实现局部整理。
不复制一个新的 attention backend 或两区域取数 kernel。

**Decode 的 DRAM → HBM sparse 访问沿用什么**

ticket01 沿用当前正式 mempool fetch：每层的 BM miss 路径调用两次 UniDexCopy。
一次读取 P prompt DRAM，另一次读取 D 生成段 DRAM。
两次调用使用同一条 miss stream，按 P、D 顺序提交。
它们写入同一个 HBM selected_kv_buffer，目标位置由 top-k 列号确定。

```flow LR
TopK位置 -> HBM槽表查询: 校验绑定及有效长度
HBM槽表查询 -> HBM命中复制: hit mask
HBM槽表查询 -> P来源复制: miss 中的 prompt 位置
HBM槽表查询 -> D来源复制: miss 中的生成段位置
HBM命中复制 -> selected_HBM: HBM 到 HBM
P来源复制 -> selected_HBM: 远端 P DRAM 到 D HBM
D来源复制 -> selected_HBM: 本侧 D DRAM 到 D HBM
selected_HBM -> SparseAttention: 等待 hit_done 和 miss_done
selected_HBM -> HBM缓存回填: 供后续步骤复用
```

图表示数据来源；P／D 两路复制在同一 stream 内串行。
HBM hit 路径使用独立 stream，可与 miss 路径重叠。
每层的 selected buffer 形状为 `[batch_rows, topk_width, 1, 576]`，类型为 BF16。
有效 top-k 比固定宽度短时，其余列使用无效 mask。

| 来源 | 选择条件 | 层内源行索引 |
|---|---|---|
| HBM sparse cache | valid 且 cache hit | 使用 slot-map 返回的 cache 位置 |
| P prompt DRAM | valid 且 miss，`0 ≤ pos < prompt_len` | `p_slot × S_P + pos` |
| D 生成段 DRAM | valid 且 miss，`0 ≤ pos - prompt_len < decode_len` | `d_slot × S_D + pos - prompt_len` |

公共 valid mask 还限制本层真正已写入的范围。
P/D 两个 mask 互斥，也与 hit mask 互斥，不会覆盖彼此的有效结果。
每个被选中的主 KV token 搬运 1152 字节，不要求搬运整页 B 个 token。
逻辑页用于分配和目录；当前 kernel 仍按 token 行索引取数。

例如 prompt_len=4096，选中位置为 `[10, 4095, 4096]`。
假设位置 10 已在 HBM cache，另外两项有效且 miss。
hit 路径填第 0 列，P copy 填第 1 列，D copy 填第 2 列。

两次 BM 调用都会保留，即使一侧没有有效项或全部 cache hit。
此时相应 valid mask 全为 false，没有有效 KV 数据搬运。
这样 Graph capture 保留固定的两路节点；replay 使用更新后的设备绑定和 mask。
这里的“两次”限定于每层的 BM miss 分支。
完整 decode 还包含主 KV 写回、HBM hit、refill 和 slot-map 更新等操作。

当前实现先提交本层新 KV 到 D mempool 的写回，再记录 copy_ready。
miss stream 等待 copy_ready，两次 BM copy 后记录 miss_done。
attention 和 cache refill 都等待 hit_done 与 miss_done。
本票继续沿用这套事件依赖；共享方案中的本步 KV 本地直供另属后续接入。

Main/SR 在请求绑定时提供 P/D 槽和长度，推进 ready 与最终释放。
每层取数由 worker 的 Runtime 和 sparse manager 在设备路径内完成。
实际调用链为：
`materialize_selected_kv → fetch_selected_kv → KVFetch.gather → SparseKVCopy.gather`。

代码依据：[两来源索引和 kernel 调用](../../python/sglang/srt/hardware_backend/npu/mempool/copy.py)、
[hit／miss／refill 事件](../../python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py)、
[attention 等待与消费](../../python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/attention.py)。

| 现有位置 | 当前行为 | 拟修改 |
|---|---|---|
| `scheduler.py:1651` | 创建 MempoolPDService | 按启动参数加载 SR，保留旧模式参考路径 |
| `scheduler.py:2114` | 每个 ingest tick 调用 advance | 纳入 SR 控制命令；保持唯一有序 TP tick |
| `scheduler.py:3290` | 把 Req 加入原生 P/D 队列 | 新模式先校验 SR dispatch，阻止绕过 |
| `mempool_tick.py:481` | 自主规划 acquire / ready / release | 新模式仅执行显式请求动作；沿用 TP 事实汇总 |
| `mempool_control.py:500` | finish_drain 归还 D 槽 | 拆开 drain 完成和显式释放 |
| `mempool_control.py:778` | DONE 触发 P 释放 | 新模式不因 DONE 归还；早到/迟到分支也覆盖 |
| `mempool_service.py:569` | 执行 drain 与 native_release | 分别保留原生资源收据和 alive 租约 |
| `decode.py:1902` | Req 到达后分配原生资源 | 采用 SR 的预留行和 prompt 索引，避免二次分配 |
| `decode.py:2527` | transfer_complete 门控 handoff | 加入 Router ready，覆盖首 token |
| `allocation.py:587` / `schedule_batch.py:3212` | 逐步分配和检查 decode 内存 | 新模式从预留中取索引、按剩余预留检查 |
| `mempool/runtime.py:223` | runtime.bind / detach_row | 复用，绑定来源改为 SR；保持稳定 Graph 缓冲 |
| `arg_groups/fields/disagg.py` | mempool 启动参数 | 增加 Router endpoint、run_id 及成对校验 |

**新模式的旧协议处理**

POOL_HELLO、heartbeat、布局校验、native transport session 校验继续使用。
它们服务于启动和数据通道身份，不决定请求槽位分配。
新模式不发送自主 ACQUIRE，也不接受旧消息改变请求所有权。
旧 DONE、RELEASE_ACK 不能转换成新 free_ack。
遇到冲突消息报告错误；可识别的迟到消息保留日志并隔离。

接管范围包括 D finish_drain、P DONE、pending_done 和取消回滚。
特别检查 finish_prefill_writes 处理早到 DONE 的分支。
新模式的 cancel / timeout 进入 fail-stop，不能沿旧回滚路径自行归还槽。

在启动时选择一种请求控制模式，避免两个 planner 同时运行。
旧正式 mempool 模式保留，用于同长度参考对照。
新模式仍通过现有 MempoolPDService hook 接入 scheduler。

## F 实现完成后如何启动

计划交付一个入口，支持 `serve` 和 `acceptance` 两种模式。
两台机器各运行一条命令，后续步骤由入口自动协调。
以下是拟交付的 CLI 契约，脚本目前尚不存在。

**运行前配置一次**

将同一份部署配置放入两侧容器，例如 `/home/cryang/mempool-min.json`。
机器地址采用最近记录的 P `10.120.72.23`、D `10.120.72.25`。
模型路径、网卡及当前机器拓扑需要按部署环境核对。
实现入口将检查路径、模型配置、可用端口、依赖及双方配置摘要。

```json
{
  "prefill_host": "10.120.72.23",
  "decode_host": "10.120.72.25",
  "model_path": "/data_lib/data/models/GLM-5.2-w4a8",
  "network_interface": "bond4",
  "numa_nodes": [0, 1, 2, 3, 4, 5, 6, 7],
  "prefill_capacity": 8192,
  "decode_capacity": 128,
  "page_size": 128,
  "context_length": 8320,
  "max_total_tokens": 16384,
  "max_prefill_tokens": 8192,
  "router_http_port": 6699,
  "router_control_port": 16700,
  "runner_control_port": 18891,
  "log_root": "/home/cryang/log/mempool-min"
}
```

以上 HBM 配置是拟定启动值，不构成容量通过证据。
模型加载后根据真实容量注册；容量不足则启动失败。
context_length 覆盖 S_P + S_D，避免存储上限与模型上下文不一致。
原生 `max_total_tokens` 表示 HBM allocator 的预算，与 BM 槽容量分开。

固定部署参数为 TP16、DP1、PP1、BF16、各 rank 私有副本。
每侧 16 个 BM 槽，DRAM 逻辑页与原生 HBM 页统一为 B token，当前部署 B=128。
P eager、D Graph，先保留既有 16 行 Graph 捕获设置。
Graph 的 padding 行与真实请求数分别检查，真实负载最多为 1。

入口显式设置 `SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD=1` 与 `SGLANG_NPU_ENABLE_MEMPOOL=1`。
设置 `SGLANG_NPU_MEMPOOL_SHARE=0`，按配置生成连续 NUMA 分配。

这只启动阶段 0 的私有后端；两个 rank 同处一个 NUMA 不表示共用一份 KV。
K1 的共享启动与映射需要后续实现，不能只把该开关改为 1。

关闭原生 radix cache，单 chunk，禁止投机与 sparse decode admit。
原生 `max-running-requests` 保留与已有 16 行路径兼容的容量配置。
实际串行约束由 Main Router 和 SR 入口共同执行。

**常驻服务：P 机**

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode serve \
  --config /home/cryang/mempool-min.json \
  --run-id ticket01-serve-001
```

**常驻服务：D 机**

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode serve \
  --config /home/cryang/mempool-min.json \
  --run-id ticket01-serve-001
```

在两个终端先后提交即可；不需要等 P 服务完全 ready 再启动 D。
P 侧入口拉起 Main Router 和 P 服务，D 侧入口拉起 D 服务。
两侧 `sglang.launch_server` 自动创建 SR；无需另启 subrouter 命令。
入口复用现有 Ascend 环境加载及进程管理方式。

底层仍使用 `python3 -m sglang.launch_server`。
拟增加 `--mempool-router-endpoint tcp://10.120.72.23:16700` 和
`--mempool-router-run-id ticket01-serve-001`。
这两个新参数尚未实现；其它 mempool 参数按配置展开并写入日志。
Main Router 拟用 `python3 -m sglang.srt.disaggregation.ascend.mempool_router` 启动。
日常使用由包装入口完成，不要求手工维护三份长命令。

**自动启动顺序**

1. 双侧入口检查环境并交换 run_id / 配置摘要。
2. P 侧启动 Main Router，暂不接收生成请求。
3. 两侧同时启动模型服务，完成 BM 映射。
4. D 完成 Graph capture，各侧挂接 SR。
5. P/D 完成原有配对身份与布局握手。
6. SR 汇总实际资源并 register。
7. Main 核对两侧完整能力，发布 ready。

BM 映射必须先于 Graph capture；控制 ready 不作为捕获前提。
不能让 P 等待 D 注册、而 D 又等待 P 模型完全 ready。
入口检查 `/ready` 时不发送真实 warmup 请求来绕过 Router。
原生 fake warmup 如有使用，也不占用用户请求配额。

**端口分工**

| 端口 | 位置 | 用途 |
|---|---|---|
| 6699 | P 机 | 新 Main Router HTTP |
| 16700 | P 机 | 新 Main Router 控制端点 |
| 18891 | P 机 | 双侧启动 / 验收入口协调 |
| 8000 / 8001 | P / D | 现有模型 HTTP，作为 Router 内部入口 |
| 8995 | P 机 | 既有 PD bootstrap |
| 19000–19015 | P 机 | 既有独立 rank-pair BM store |
| 25670–25701 | 两机 | 既有 BM NIC 地址范围 |

原生 TransferEngine / HCCL 等其它端口仍由既有启动路径管理。
新模式使用新 Main Router，不在同一阶段启动旧 mini_lb。

服务就绪后，客户端只访问 `http://10.120.72.23:6699`。
输入文件应由真实模型 tokenizer 生成并满足页对齐。
示例文件 `request.json` 包含 input_ids、stream=false 和 greedy sampling_params。

```bash
curl --fail-with-body http://10.120.72.23:6699/ready
curl --fail-with-body http://10.120.72.23:6699/generate \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

**自动验收**

两机将上面命令改为 `--mode acceptance`，使用相同的新 run_id。
完整拟定命令如下，仍需等待脚本实现。

```bash
# P 机
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode acceptance \
  --config /home/cryang/mempool-min.json \
  --run-id ticket01-accept-001

# D 机
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode acceptance \
  --config /home/cryang/mempool-min.json \
  --run-id ticket01-accept-001
```

入口先跑既有正式 mempool 参考组，再跑新 Router 目标组。
两组顺序启动和结束，避免同时占用模型、端口和池。
参考组与目标组使用相同模型、输入、Graph 配置、容量和采样设置。

验收入口自动生成输入、等待服务、发请求、检查输出、核对事件、收集报告并清理。
失败返回非零。报告存放在 `/home/cryang/log/mempool-min/<run-id>/`。
P 侧汇总两侧材料；保留 `prefill/`、`decode/`、`router/` 与 summary.json。

正常停止和失败清理只处理本次启动的进程组。
失联或强制终止不能记作成功 drain 或 free_ack。
另提供 `--describe`，在没有 NPU 的环境中查看展开的启动计划。

## G 怎样证明方案实现正确

**CPU / 静态检查**

| 测试入口 | 应验证的外部行为 |
|---|---|
| RouterCore + 双侧协议驱动 | 完整 cold 流程、四类账本、重复回执、P/D 不同槽 |
| 实际控制 socket + SR tick 驱动 | 消息入队、全 rank 一致、延迟 rank、注册不全 |
| 原生预留适配 | 分配不足、预留消费、实际尾页、提前 EOS、未使用整页归还 |
| service / scheduler hook | 未获 dispatch 不入队，三条件不齐不返回首 token |
| 显式释放 | 无 free 不复用；旧 DONE 不释放；free_ack 不提前 |
| 运行入口 | 配置展开、就绪判断、进程归属、失败退出、报告收集 |

allocator 行为测试不能只断言方法被调用。
需要核对分配前后可用行/页、真实索引归属和无重复归还。
预留后的全局 free 较少，也不能误触发原生 retract。
新模式与旧参考路径均要运行适用的回归检查。

**用户执行的 NPU 验收**

- 两个内容不同的 4096-token 冷请求，各允许最多生成 128 token。
- 每个请求完整 output_ids 与同配置参考完全一致。
- 存在真实 D decode forward 和 mempool 读取。
- P/D 来源、Graph replay 和 padding 行符合 ticket 要求。
- P 原生资源可提前归还，之后 D 仍正确读取 P alive。
- D finished 发生在最后访问及原生资源归还之后。
- free_ack 发生在实际归还之后，四项账本恢复。
- 第二请求复用上一请求真实归还的最小空闲 mempool 槽。
- 输入拒绝、超时和 fail-stop 的检查有独立结果。

主验收设置应确保覆盖真实 decode。
可用 `ignore_eos=true` 固定生成长度，参考组采用相同设置。
另测自然 EOS 和 max_new_tokens=1 的资源清理行为。
报告区分 smoke 与完整 ticket01 验收，不宣称 prefix 已覆盖。

每条事件记录 run_id、代码版本、rid、op_id、rank 和槽租约。
因果关系通过命令与回执身份确认，不仅比较跨机时间戳。
硬件证据由用户执行后回传，再决定是否关闭 ticket01。

## H 实施顺序与代码依据

实施仍属于一个 ticket，按以下可检查步骤推进。
每一步覆盖真实接口行为，再继续下一个步骤。

| 步骤 | 交付内容 | 检查重点 |
|---|---|---|
| 1 | Main 事件逻辑、消息类型、SR 注册与 tick 接入；整理旧控制副作用 | 全 rank 注册、容量与输入拒绝；新旧控制模式互斥 |
| 2 | 显式 alloc / dispatch 和 D 原生预留 | 真实行页分配、唯一槽所有权、避免双重分配 |
| 3 | 接通原生 HTTP、prefill、ready 与 decode | 输入身份一致、首 token 门控、两侧数据路径 |
| 4 | finished、free、free_ack 与槽复用 | 原生与 alive 分离、所有旧自动释放分支 |
| 5 | 双机 serve / acceptance 入口与报告 | 启动依赖、完整参考对照、自动清理 |
| 6 | 中文说明、代码审阅、用户 NPU 验收 | 行为与规格一致，证据完整后才闭票 |

实现中最需要验证的三个位置：

- 原生预留的消费与清理，尤其实际尾页和提前结束。
- SR 集体通信与原有 scheduler tick 的一致顺序。
- 旧 drain / DONE / 迟到消息分支不再绕过显式 free。

当前没有运行新实现的 CPU 或 NPU 测试，因为本轮没有实现代码。
新 CLI、端口和模块划分属于此方案的建议，尚未替代已发布规格。
ticket01 继续保持 ready-for-agent / open。

2026-10-23 本次补充了内存几何、记录形状和原生预留细节。
核对代码基线为 `3883624810` 加当前工作区。
拟新增的 Router／SR 模块与 run_mempool_min 脚本尚不存在。
本次检查仅覆盖文档、代码事实及容量算术，不计为功能通过。
目标页 D 继续记录待实现、待用户 NPU 验收。

**现有代码依据**

| 依据 | 本轮核对内容 |
|---|---|
| [本地规格](spec.md) | 角色分工、资源预留、输入范围、显式释放 |
| [独立 ticket01](issues/01-cold-request-lifecycle.md) | 冷请求、复用、验收与排除项 |
| [KVLayout / PoolLayout](../../python/sglang/srt/hardware_backend/npu/mempool/layout.py) | layer-first 几何、字节偏移及 BM 对齐 |
| [MempoolKVManager / View](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | rank-pair 建池、NUMA 分配及本进程地址解析 |
| [MempoolRuntime](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 稳定绑定张量、Graph padding、写回事件及收据 |
| [现有协议类型](../../python/sglang/srt/disaggregation/ascend/mempool_protocol.py) | RequestIdentity、SlotLease、原生 Index K 布局 |
| [MempoolPDService](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 模型加载后挂接、native_release、transfer_complete |
| [MempoolTPTick](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py) | 有序 TP 事实汇总与旧自主动作 |
| [MempoolPDControl](../../python/sglang/srt/disaggregation/ascend/mempool_control.py) | 租约身份、finish_drain、DONE 与 RELEASE_ACK |
| [Scheduler](../../python/sglang/srt/managers/scheduler.py) | ingest_requests、原生请求入队与空闲 tick |
| [Decode](../../python/sglang/srt/disaggregation/decode.py) | 原生 prealloc、handoff 门控 |
| [原生行池](../../python/sglang/srt/mem_cache/memory_pool.py) | alloc_rows / free_rows |
| [页分配器](../../python/sglang/srt/mem_cache/allocator/paged.py) | 按 token 数传入 alloc；返回实际 token 索引 |
| [逐步分配](../../python/sglang/srt/mem_cache/allocation.py) | alloc_for_decode 的索引与有效长度更新 |
| [原生清理](../../python/sglang/srt/mem_cache/common.py) | release_kv_cache 与 overallocated 约束 |
| [现有 Python mini_lb](../../sgl-model-gateway/bindings/python/src/sglang_router/mini_lb.py) | 同 room 分发两侧 HTTP、收取 D 响应 |
| [现有启动脚本](../../ascend-mempool-test/scripts/run_service.sh) | Ascend 环境、模型参数、端口与日志 |
| [现有自运行入口](../../ascend-mempool-test/scripts/run_ticket10.py) | 双侧协调、参考对照和进程管理 |
| [硬件验收规则](../ascend-mempool/verification.md) | 用户运行 NPU 测试，验收后闭票 |
