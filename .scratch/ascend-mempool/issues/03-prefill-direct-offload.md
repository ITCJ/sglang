# 03: 正式 mempool 数据路径 cutover

**What to build:** 在02已验收的真实 shadow 双写/控制/readback 基础上，让 D attention
实际消费 mempool sparse fetch 的 KV；按模式停用原 main compact-KV transfer、staging
和长期 host KV allocation。原非mempool实现保留，关闭mempool后同一版本可恢复原
sparse PD路径。保留 P native HBM cache、HBM Index K 及必要辅助传输；S5验收真实
server Graph、curl小题目输出与性能，S6完成全量review、shadow删除及清理后复验。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [02: 16 对 rank 的正常控制闭环](02-rank-pair-control-lifecycle.md).

**Status:** ready-for-agent

**State:** open

## 规划状态（2026-10-06）

02已获用户确认验收并关闭，已有链路、代码入口及证据见[02总结](../ticket-02-summary.md)。
用户已确认按S1–S6组织本票，并先后授权实施S1和S2。S1代码已提交为`3d2f3c6168`，轻量CPU检查通过，
完整SGLang环境的资源/启动单测及NPU回归待执行；见[S1交付总结](../ticket-03-s1-summary.md)。
S2代码和独立NPU gate已提交为`9036be2b0f`；首次K=8参数错误修正为2048后，
双机30个case及正常退出均通过，用户于2026-10-04确认S2独立NPU gate无问题；见
[S2交付总结](../ticket-03-s2-summary.md)。用户随后授权先提交当前修改，再实施S3；S2修正与
验收记录已提交为`dd1f92f618`。S3代码已提交为`5b18a8046c`，Mac检查通过；用户于
2026-10-05确认S3 NPU资源gate通过，见[S3交付总结](../ticket-03-s3-summary.md)。
S4代码已提交为`8e00b36cf9`，用户于2026-10-05回传双机六个case全部通过的日志，
并确认“S4完成”；见[S4总结](../ticket-03-s4-summary.md)。
下一阶段的范围和验收安排见[S5计划](../ticket-03-s5-plan.md)及
[S6计划](../ticket-03-s6-plan.md)。2026-10-05用户确认本次S5/S6调整并授权实施S5。
S5正式入口、完成证据、连续异步gate和服务运行说明已实现。用户已回传一次真实
server请求：全16 ranks完成512次Graph replay及slot释放；2026-10-06双机连续五步
异步组件gate的30个case全部通过。同日三个真实服务请求及全rank日志checker通过，
覆盖zero-decode、连续decode、slot新generation复用与安全释放。关闭thinking后小题目输出检查通过；用户确认当前性能无问题。
S5按已验证的小容量范围验收通过，长上下文容量由用户独立跟进；本票仍待S6完成。原KV传输、D staging/host写入及sparse
attention的host SHM读取在正式模式关闭、在普通模式保留。
当前MEMPOOL=1选择正式P/D模式；S6.2已删除shadow模式、旧READBACK配置及相关实现，
有效测试迁移到正式/普通路径。S6.3三个STD代码整改完成，最终全范围复查和NPU复验待执行。
继续使用context1024、P/D各512、TP16、D Graph width16、NUMA `0,2,4,6`的小容量配置。
大容量/NUMA调查仍归延期的[09](09-numa-allocation-followup.md)，不阻塞本票。

本票交付完整的正式数据路径、小容量真实服务Graph验收和约定性能验收；输出正确性
由用户用curl发送一个小题目并核对回答，不加入AIME26等正式数据集的完整精度验收。
[04](04-single-request-graph-decode.md)的现有范围保持不变；05–08继续承担并发、
容量/取消、故障和AIME26验收。共享路径保留原非mempool模式。

### 已确认的启动模式与清理边界

现有 `SGLANG_NPU_ENABLE_MEMPOOL=1` 在S5选择正式路径。S6.2已删除shadow专用enum、
分支、旧host参考readback、配置及配套脚本/测试代码，历史验收记录和git提交保留。
正式路径共用的BM writer、binding、Graph和drain功能继续保留。

关闭 `SGLANG_NPU_ENABLE_MEMPOOL` 后，同一版本必须恢复原非mempool路径；启用
sparse offload时，P/D分别选择原 `PD_PREFILL_NATIVE` / `PD_DECODE_OFFLOAD`。
恢复普通路径不需要切回02提交，也不依赖保留shadow双写模式。

正式路径的正确性不依赖 `SGLANG_NPU_MEMPOOL_READBACK`。S5在正式模式提前拒绝旧
READBACK组合；S6.2已删除该shadow专用配置及实现，并清理启动脚本中的旧设置。独立已知
pattern的copy验证、正式fetch范围检查和完成证据继续保留，不将BM与自身比较。

## 目标数据路径与资源清单

```text
P temporary compact KV -> P native HBM（prefill attention）
                       -> P BM（持有到 D 完成读取）

D temporary compact KV -> D BM

HBM Index K -> indexer top-k -> HBM sparse cache 查询
  hit  -> HBM cache --------------------+
  miss -> 按 prompt length 读取 P/D BM --+-> selected KV -> attention
                                         -> HBM cache refill
```

| 资源或行为 | 03的处理 |
| --- | --- |
| P native HBM KV | 保留，供prefill计算；按02安全条件回收 |
| P prompt BM / D decode BM | 保留现有writer、binding、slot ownership和drain |
| D HBM Index K | 保留分配、页索引、indexer和必要PD传输 |
| D HBM sparse cache、slot map、selected buffer | 保留hit/miss/refill/reset及attention消费方式 |
| 旧长期compact-KV hostSHM及映射 | 正式模式不分配、不注册、不写入、不读取；实现保留 |
| main compact-KV的PD注册与传输 | 正式mempool模式停用 |
| main-KV专用D staging及staging到host copy | 正式mempool模式停用 |
| 必要state/aux/metadata及传输完成、失败状态 | 继续工作；D仍等待其Success与matching KV_READY |

## 实施事项与顺序

下文路径相对 `python/sglang/srt/`；`npu/` 表示 `hardware_backend/npu/`。
按S1→S2→S3→S4→S5→S6推进，每项完成条件均须满足，最终一起交付本票。

| 步骤 | 交付结果 | 当前进度 |
| --- | --- | --- |
| S1 拆分配置与资源职责 | 明确运行模式、派生能力、资源归属及初始化合同 | 代码已实现；轻量CPU通过，完整环境及NPU回归待执行 |
| S2 接入正式BM fetch | attention消费BM miss结果，保留HBM hit/refill | 实现已核对；K=2048独立NPU gate的30个case通过，用户已确认 |
| S3 按模式停用重复存储 | 旧实现保留；正式模式host KV和main-KV staging分配为零 | 已提交；Mac检查通过，用户于2026-10-05确认NPU资源gate通过 |
| S4 精简PD传输 | 仅保留Index K和必要辅助数据，保留联合readiness | 双机六个case全部通过，用户于2026-10-05确认完成；见[S4总结](../ticket-03-s4-summary.md) |
| S5 正式服务Graph与性能验收 | 完整mempool链路、curl小题目检查、约定性能达标 | 组件gate、服务checker及小题目通过，当前性能获用户确认；S5已验收，见[总结](../ticket-03-s5-summary.md) |
| S6 全量review、shadow删除与清理 | 精简代码、普通模式兼容、最终版本重跑S5并交付 | S6.1–S6.3完成，最终全范围复查和NPU复验待执行；见[报告](../ticket-03-s6-review.md)及[计划](../ticket-03-s6-plan.md) |

### S1. 拆分配置与资源职责

#### 当前耦合与拆分目标

S1实施前，`npu/sparsity_driven_kv_offload/config.py::SparseKVOffloadMode` 已有
`DISABLED`、`LOCAL_OFFLOAD`、`PD_PREFILL_NATIVE`、`PD_DECODE_OFFLOAD`。
其中 `uses_host_kv_offload` 同时被用于三个不同判断（S1已改用右列能力）：

| 当前调用位置 | 实际决定的行为 | 建议改用的含义 |
| --- | --- | --- |
| `npu/attention/ascend_backend.py` | 创建sparse manager并选择sparse attention | 是否使用HBM sparse cache路径 |
| `npu/memory_pool_npu.py` | 不分配完整native compact K/V buffer | 是否由sparse cache替代完整HBM KV存储 |
| `npu/sparsity_driven_kv_offload/config.py::get_sparsity_driven_kv_offload_cell_size()` | native token pool按Index K计算每token容量 | 是否采用sparse设备存储布局 |

正式mempool D需要继续满足上述三个条件，同时不再使用旧host backing。
只把 `uses_host_kv_offload` 设为false会改变attention分支、恢复完整HBM K/V分配，
并改变容量计算；只保留为true又不能表达旧host存储应停用。

#### 配置建议：运行模式推导能力

沿用现有配置入口，由sparse开关、mempool开关及P/D角色解析运行模式。建议扩展
现有enum，增加 `PD_PREFILL_MEMPOOL`、`PD_DECODE_MEMPOOL`，避免P的数据计算路径
和传输策略由不同位置各自猜测。关闭mempool时保持已有模式；`DISABLED`继续表示
sparse功能关闭。下表为03正式路径的目标合同，02历史shadow模式不等同于正式模式。

| 运行模式 | HBM sparse cache | 旧host KV | 完整native compact KV | main-KV PD传输 | D main-KV staging | BM |
| --- | --- | --- | --- | --- | --- | --- |
| `LOCAL_OFFLOAD` | 有 | 有 | 无 | 不适用 | 无 | 无 |
| `PD_PREFILL_NATIVE` | 无 | 无 | 有 | 有 | 无 | 无 |
| `PD_DECODE_OFFLOAD` | 有 | 有 | 无 | 有 | 有 | 无 |
| `PD_PREFILL_MEMPOOL`（建议新增） | 无 | 无 | 有 | 无 | 无 | 本地P池及配对映射 |
| `PD_DECODE_MEMPOOL`（建议新增） | 有 | 无 | 无 | 无 | 无 | 本地D池及配对映射 |

所有表内DSA模式仍保留所需HBM Index K；正式P/D仍传输Index K及必要辅助数据。
“main-KV PD传输无”只指原compact-KV发送任务，D读取P BM仍有实际跨机数据访问。

P native compact-KV是否分配/写入，与是否进入PD注册/发送列表是独立决策：正式P仍为
prefill attention保存native HBM KV，但只向原transfer路径发布Index K及必要辅助buffer。
不能根据“native K/V buffer存在”推断应传输它，也不能因不再传compact KV就关闭整个
sender/handoff。该控制合同及代码入口见S4的P端补充。
当前BM实现分配的是DRAM（`mempool/manager.py`中`local_hbm_size=0`），映射为设备
可访问地址不改变其物理存储类型；P原生HBM page cache与P BM是两份独立存储。

建议内部属性如下；它们从mode推导，不新增四个可独立设置的用户开关：

- `uses_sparse_kv_cache`：LOCAL、旧PD D及正式mempool D为true；控制cache/attention、
  native compact-KV分配选择及相应容量计算。
- `uses_host_kv_offload`：只表达旧host backing，LOCAL和旧PD D为true。
- `uses_pd_decode_staging`：仅旧PD D为true，保留现有属性名与准确含义。
- `uses_mempool_bm`：正式mempool P/D为true；BM接入及Index K传输策略据此选择。

`MempoolConfig` 继续负责BM容量/layout、通信地址及demo组合约束；模式选择集中在
sparse配置入口。两处复用现有校验，不各自维护一套P/D分支或新增通用资源管理层。
S1实施时保留的shadow模式仅用于过渡；按2026-10-05确认的范围，S6统一删除。
`READBACK`不得决定attention的数据来源或是否分配旧host KV。

#### 资源职责与初始化顺序

| 模块 | 应负责的资源或行为 |
| --- | --- |
| `NPUMLATokenToKVPool` | P native compact KV和P/D Index K的物理buffer、页布局；向PD提供相应buffer描述 |
| `SparseKVCacheManager` | D HBM cache、slot map、hit/miss/refill、stream/event和请求reset；仅旧模式拥有host backing及main-KV staging buffer |
| sparse attention | 创建本次eager/capture的selected buffer，交给materialization填充，等待完成后供attention消费 |
| `MempoolKVManager` | 进程级BM池、映射、layout和地址；request释放不销毁BM池 |
| `MempoolRuntime` | row binding、固定设备metadata、BM writer/fetch及完成事实；不接管HBM cache或协议状态机 |
| Ascend PD manager/staging adapter | 按模式注册和传输buffer、必要的staging slot管理及原transfer完成/失败事实 |
| mempool control/service/tick | slot/generation ownership、准入、drain与释放次序；协调原allocator回收native资源 |

Sparse manager建议接收已经解析的mode，在自身实现中按能力创建所需资源。
它不负责创建BM runtime，也不解析PD消息；mempool runtime继续由backend引用，
因此P即使没有sparse manager也能正常写BM。正式fetch的具体接口留给S2。

现有 `model_executor/model_runner.py::init_attention_backends()` 先构建backend，
backend构造时已经创建sparse manager，之后才调用 `initialize_for_model_runner()`
创建并attach mempool runtime。因此不能以“runtime现在是否非空”决定host分配。
目标初始化顺序为：

```text
解析运行模式与配置校验
  -> 按模式估算容量、创建native pool/Index K
  -> 创建backend及需要的HBM sparse cache
  -> 创建BM映射/runtime并attach
  -> Graph capture
```

模式及能提前确定的参数约束在相关KV资源分配前校验；依赖实际buffer/layout的检查
仍在attach时执行。正式D在capture/首次forward前必须已有可用BM runtime，缺失时
明确失败。配置校验不导入MemFabric或创建BM连接。
`max_running_requests`仍须有界，因为HBM sparse cache和row表也按请求容量分配；
停用旧host allocation不意味着取消此限制，原来只提host allocation的报错需修正。

S1先完成模式、能力判断和初始化合同的拆分；正式模式跳过host/staging分配由S3落实，
PD注册/传输切换由S4落实。阶段提交应保持可运行：在S2–S5接通前，不把现有启动
开关导向半完成的正式路径，也不通过静默回退旧host路径掩盖未接通状态。

完成条件：CPU模式矩阵验证新旧角色的能力、native分配与容量选择，覆盖mempool开
而sparse关、非PD角色等非法组合；配置决策不依赖runtime是否attach或readback开关。
资源归属明确，后续S3/S4可依据同一mode停用重复分配/传输并保留旧实现；S1本身不
宣称正式fetch或host/staging零分配已完成。

#### 旧路径保留与模式切换合同（2026-10-03用户补充）

“关闭”同时包含资源和执行入口：正式模式不创建旧host SHM及main-KV staging，不
注册/传输main compact KV，不提交旧host写入和host miss读取。相关函数、类和普通
模式的调用链保留在同一代码版本，不能用删除旧实现、空函数或永久返回替代模式选择。

- 运行模式在进程启动时解析，并保存在pool/backend/manager中。分配、注册、writer、
  miss来源和cleanup依据同一模式；不增加可任意组合的“关传输/关host读/关host写”开关。
- 正式模式的host读取入口不可用。若意外进入 `offload()`、`offload_v2()`、
  `get_forward_kv()` 或staging创建/host copy入口，应给出明确的模式错误，不能临时
  分配host buffer或静默回退。正常调用方应先按模式分流；空资源的clear/free可安全执行。
- P/D必须采用兼容模式和传输契约；在注册/发送前核对各自buffer清单，首次数据发送前
  完成peer契约检查。配置不一致或BM runtime未接通时明确失败，不尝试普通路径降级。
- 模式不在服务运行中热切换。切换前停止接入新请求，等待在途工作drain并释放，再以
  新配置重启P/D，重新分配、注册和capture Graph；修改环境变量不改变已捕获的Graph。
- `MEMPOOL=0`不要求初始化BM连接或加载MemFabric运行依赖；普通模式仍用原host读取。
  `READBACK`只控制诊断，不能改变资源分配或attention的数据来源。

#### 按代码入口接线：保留旧分支，增加正式分支

以下均为待实施设计；路径前缀与上文相同。

| 代码入口 | 模式选择与保留方式 |
| --- | --- |
| `npu/sparsity_driven_kv_offload/config.py` | 扩展现有enum及派生属性；旧模式含义保留，正式P/D增加明确模式 |
| `npu/attention/ascend_backend.py`、`npu/memory_pool_npu.py` | 用 `uses_sparse_kv_cache` 决定D sparse attention/cache及容量布局；P正式模式继续分配和写native HBM KV |
| `npu/sparsity_driven_kv_offload/manager.py::__init__()` | HBM cache/slot map及共用event照常创建；旧SHM分配与映射代码放在 `uses_host_kv_offload` 分支；host专用成员以空列表或 `None` 表示未创建 |
| `npu/sparsity_driven_kv_offload/attention.py::forward_sparsity_driven_kv_offload()` | `save_kv_cache` 且 `uses_host_kv_offload` 时才调用旧 `offload_v2()`；保留该实现；正式D继续使用backend已有BM writer，不在此重复写BM |
| `npu/sparsity_driven_kv_offload/manager.py::materialize_selected_kv()` | 共享HBM lookup/hit/refill/map逻辑，仅miss分支选择旧host SHM copy或新P/D BM fetch |
| `npu/memory_pool_npu.py::get_contiguous_buf_infos()` / `get_kv_layer_ids()` | 正式P/D只发布Index K对应buffer描述和实际layer IDs；普通模式保留原K/V+Index K列表；P native K/V仍存在但不在正式传输列表中 |
| `disaggregation/ascend/sparse_pd.py` 与 `npu/memory_pool_npu.py` 的staging创建入口 | 仅 `uses_pd_decode_staging` 为true时创建adapter和K/V staging；保留原类及创建代码，正式D不调用 |
| `disaggregation/ascend/conn.py` | 普通模式保留索引重写、K/V+Index K发送及Success后的staging→host copy；正式模式使用native Index K页索引及对应发送分支，继续传递原Success/Failed |
| sparse manager的request allocation/reset/clear及PD release hooks | 始终清理共用HBM cache映射；host/staging专用清理仅在资源已创建时执行，保留既有native/BM释放次序 |

为接入新miss来源，可在 `materialize_selected_kv()` 增加仅关键字的可选runtime参数，
由attention从backend传入。mode决定分支；参数只是依赖，不能用“runtime非空”代替
模式选择。正式模式在capture前验证依赖存在，普通模式不需要runtime。无需复制整套
manager/attention，也不引入通用storage provider框架。

materialization的目标顺序为：

```text
共用：根据本模式可读范围构建valid mask，查询HBM cache，生成hit/miss mask
  hit stream  -> 现有HBM cache copy -------------------------------+
  miss stream -> 普通模式：现有host SHM copy                         |
              -> 正式模式：P BM copy + D BM copy（只写miss位置）------+-> selected KV
共用：hit_done / miss_done -> attention消费；refill stream回填HBM cache
```

当前 `_build_miss_src_dst_index()` 计算的是host布局索引，正式分支须跳过这一构建，
把请求row、top-k位置和miss mask交给BM runtime按binding转换；不能把host index直接
当作BM地址。miss stream先等待共用 `copy_ready`；正式分支在同一stream依次提交P/D
copy（也满足S5的BM writer依赖），两路完成后统一记录 `miss_done`，保持attention与
refill的等待合同。capture时即使有效miss为零也记录两路copy。后续若分成两个stream，
必须先汇合两路完成事件再发布 `miss_done`，本票无需为此增加并行结构。

### S2. 把已验证的BM读取接入attention，并保留HBM cache

- 在 `npu/mempool/runtime.py` / `copy.py` 提供正式fetch入口，接受layer、request rows、
  selected positions、miss mask和目标buffer；接口不引入Req或另一套协议状态。
- 将D的prompt slot校验、`row_decode_len`更新和读取范围检查从readback条件中独立出来。
  可读范围按实际forward写入进度确定，包含本层当前写入；不从输出token数推算。
- `npu/sparsity_driven_kv_offload/manager.py::materialize_selected_kv()` 保留HBM
  lookup/hit/refill/slot-map流程，将miss来源改为远端P BM和本地D BM。
  两路copy写入selected buffer中各自的miss位置，不能覆盖并行写入的hit位置。
- 统一有效性规则覆盖hit、miss、refill和slot-map更新：排除padding/未绑定row，
  检查prompt/decode实际写入范围。真实非负越界必须报告错误，不能悄悄mask后缓存空值。
- attention已生成padded `topk_2d`，但当前materialization仍接收原始 `topk_indices`。
  正式路径统一使用与selected buffer行宽一致的形状，并把有效性传到attention长度计算；
  验证短top-k、batch>1和row 0 padding。缓存copy对象时还须核对目标buffer身份，
  避免后续eager/capture写入上一次的selected tensor。
- `npu/sparsity_driven_kv_offload/attention.py` 实际消费该selected buffer；正式读取
  不经过 `KVReadback.compare()` 的诊断scratch来间接提供数据。

完成条件：可分别验证P miss、D miss、HBM hit及refill后的再次命中；关闭readback时
仍使用正确binding和写入长度；读取逻辑复用已有P/D路由，attention输入确实来自新路径。

### S3. 按模式停用旧host KV与main-KV staging，保留实现

- 在sparse manager中选择性跳过 `host_kv_buffer`、host/device映射及仅服务旧host路径
  的元数据；保留HBM cache、必要stream/event和请求reset接口。
- 仅正式模式停止调用旧 `offload_v2()` 写host KV，D的新KV沿02已有writer写入本地BM。
  保留普通模式的offload及 `get_forward_kv()` 路径；正式模式的decode不得落入旧host
  prefix读取分支，未支持的forward组合明确拒绝。核对所有host读写入口及S1模式保护。
- 在PD适配入口跳过main-KV staging pool、K/V staging buffer分配、目标索引重写和
  staging→host copy；D仍为Index K分配真实device pages。
  两个创建入口分别位于 `npu/memory_pool_npu.py` 和
  `disaggregation/ascend/sparse_pd.py`，均须覆盖。
- 核对request allocation/free/clear hooks。新请求复用row或BM slot时清理旧cache映射；
  host元数据缺席不能导致cleanup报错，原native回收仍由02的service安排。

完成条件：正式模式下旧hostSHM和main-KV staging实际分配量为零；HBM cache及Index K
仍有效。不能只跳过copy而保留原大块分配，也不能通过禁用整个manager完成此项。
关闭mempool后，旧SHM/staging分配、写入和读取实现均能恢复运行。

### S4. 将PD传输改为仅保留必要的Index K/state/aux/metadata

2026-10-05核对现有代码后的逐文件改动、peer契约与测试方案见
[S4实现方案](../ticket-03-s4-plan.md)。该文档已按最终5个Ascend/NPU文件方案更新；代码交付不等于硬件验收通过。
用户要求优先修改Ascend/NPU目录，随后明确选择原KVArgs，并取消AscendKVArgs及
`ascend/args.py`。当前方案在既有Ascend发送入口按模式分流，模式由已有service/control
从真实pool取得，不通过新KVArgs字段传递；工厂、公共KVArgs和共享worker均不改。
为使main-KV注册量为零，NPU pool在注册前仅发布正式模式所需Index K条目。
共享utils保持原样：service构造时在receiver发布之前补齐原KVArgs的实际层号/组数。
布局检查放在Ascend本地、握手与既有发送入口，原页索引、空末chunk及失败/drain复用。

- 核对 `npu/memory_pool_npu.py::get_contiguous_buf_infos()` 及
  `disaggregation/ascend/conn.py` 的buffer列表：当前NPU MLA把K组、V组和Index K
  尾部一起放入 `kv_data_ptrs`；`setup_state_kv_args()` 不另建NPU MLA的DSA state条目。
  因此不能清空 `kv_data_ptrs` 或跳过整个 `send_kvcache()`。
- 让P/D发布并匹配明确的Index K entries、实际layer IDs、item/page步长和native
  device indices；仅正式模式的发布列表排除main K/V entries，原构造/发送分支保留。
  保留其他真实存在的state/aux/meta入口。
  不沿用“前2×layers必为K/V”的staging假设，也不把staging indices用于Index K。
  `get_state_layer_ids()` 给出实际indexer层顺序；`disaggregation/utils.py` 当前的
  group/layer推断不完整覆盖该布局，须明确逐项匹配，支持非连续Index K layer IDs。
- 继续使用既有transfer worker的完成、失败及abort drain事实。
  D decode准入仍要求原transfer Success与matching KV_READY共同满足。
  原worker的 `_staging_outstanding` 还统计传输chunk，保留其Index K/aux drain用途，
  不能因为同名D staging被停用就一起删除。
- 为改变后的传输语义加入明确的peer兼容性检查，覆盖旧shadow版本/正式版本混用
  或两端配置不同；只比较BM容量布局不足以证明传输buffer契约一致。
  建议升级mempool payload协议版本、保留旧router可识别的路由tag，在现有版本检查处
  拒绝旧peer。若保留同版本两种模式，还须校验所选路径。实际发送前精确检查entry数、
  ordered layer IDs和Index K页步长；不能依赖允许多余entry的通用layer配对掩盖不匹配。

完成条件：两端注册列表及实际发送任务中main compact-KV为零，Index K/aux仍按正确
页索引传输。两种readiness先后顺序均可推进；传输失败或契约不匹配不得提前放行。

#### P端保留page cache时的handoff合同（2026-10-03核对）

P保留native HBM KV本身不阻碍控制流；风险在于把“移除compact-KV payload”实现为
“不再调用原sender”。当前完成、首token交接和native资源回收仍依赖以下链路：

| 控制环节 | 当前代码入口 | 正式模式必须保留的行为 |
| --- | --- | --- |
| bootstrap及总page数 | `disaggregation/prefill.py::finalize_bootstrap()` | 初始化sender及metadata buffer；总page数仍对应需要发送的Index K页 |
| chunk与末chunk识别 | `disaggregation/prefill.py::_send_kv_chunk()`、`disaggregation/common/conn.py::CommonKVSender._prepare_send_indices()` | 保留native page indices及累计计数；只过滤buffer列表，不清空Index K页索引 |
| 真实handoff完成 | `disaggregation/mooncake/conn.py`的transfer worker | 发送Index K，最后chunk发送state/aux；成功后走`conclude_transfer()`更新P状态并通知D |
| P离开inflight队列 | `disaggregation/prefill.py::process_disagg_prefill_inflight_queue()` | 继续根据真实sender Success结束请求，调用`defer_release(req, handoff=True)` |
| D准入 | `disaggregation/utils.py::_apply_metadata_gate()`、`disaggregation/decode.py::pop_transferred()`及`MempoolPDService.transfer_complete()` | 保留metadata到达检查、TP一致性及Success与KV_READY联合准入 |
| P native回收 | `disaggregation/ascend/mempool_service.py::_observations()`及`_free_native()` | 等handoff与本地BM写入完成，再detach row并由原allocator回收native页和metadata |

上述函数名须在实施时与代码保持一致。尤其sender的`is_last_chunk`来自
`curr_idx == num_kv_indices`，不是直接采用scheduler传来的`last_chunk`。
若总page数仍为正却改为发送空indices，计数无法到达末chunk，aux/Success不会走正常
完成分支；若直接跳过send，则P inflight和D transfer queue都可能持续等待。
不得在BM写完时手工合成原transfer Success：Index K和首token metadata可能尚未送达。
metadata未到时，D现有gate还会把Success降回Transferring；仅设置状态不能完成handoff。

正常完成时序保留为两条独立进度汇合：

```text
P prompt BM写入全部完成 -----------------> matching KV_READY --+
                                                             +-> D准入
P Index K各chunk -> 末chunk state/aux -> Success与metadata到达 --+

P handoff完成 + 本地BM写入完成 -> detach row / native页回收
D最后读取完成并drain -> DONE -> P确认本地安全与detach -> P BM slot释放 / ACK
```

P native页回收后，旧请求的P BM slot仍须保留给D；不能把两种释放绑定在一起。
取消/失败路径继续保留transfer worker计数与abort drain：即使没有compact-KV推送，
Index K和aux仍可能访问P源buffer或D目标buffer。

新增验证要求：main compact-KV发送字节数为零时，真实Index K/aux发送仍使P队列退出、
D正确接收首token并进入decode；覆盖多chunk计数、末chunk无新增页但需发送aux、
BM先ready/transfer先完成两种顺序、metadata延迟、P native row复用而旧BM slot仍占用。
这些属于03待实现的集成验证，02已有控制测试通过不能替代本项。

### S5. 正式服务Graph、输出检查与性能验收

**Blocked by:** S4实现及用户NPU验收（已完成）。

**交付行为：** 在真实SGLang P/D server和NPU Graph replay下运行完整正式mempool
链路；用户用curl发送一个小题目检查回答；在预先约定的负载和指标下性能达标。
详细接线、测试与证据要求见[S5计划](../ticket-03-s5-plan.md)。

- 开放正式启动入口，复用02的forward/drain hooks和S2双源copy，修复真实服务接线
  缺口。正式模式关闭旧host/main-KV路径，保留Index K/必要辅助传输和联合readiness。
- Graph保留P/D两个copy，包括zero-valid和padding；固定metadata地址，每次replay
  使用当前slot、长度和indices。连续异步提交验证不能依赖逐token host同步。
- 核对BM write、hit/miss、attention、refill及slot-map更新的stream依赖，完成事件
  和whole-D drain覆盖全部KV访问；native row回收与BM slot释放遵守各自持有期限。
- 覆盖真实decode、zero-decode、连续请求、row/物理slot复用及全rank资源归还。
  按实际提交/完成的forward核对容量，不固定假设输出N个token等于N-1或N+1次KV写入。
- 交付可执行的P/D启动、curl请求、日志收集与性能测量步骤；本阶段就提供足够的
  正式模式证据，不能把S5所需观测全部推迟到S6。组件gate补充证据，不能代替server验收。
- 输出检查由用户核对一个小题目的预期答案和实际回答。记录请求参数、输入/输出及
  判断结论；本阶段不要求正式数据集、完整精度评分或逐token baseline一致。
- 性能使用相同模型、硬件、输入/输出长度、采样、并发和Graph配置，明确基线与
  预热口径，记录TTFT、TPOT/ITL和吞吐；同时记录必要的资源与drain开销。
  **用户验收时自行查看实测TTFT、TPOT和输出吞吐并判断，当前不要求预先提供阈值**。
  获用户确认后才能记录性能通过，也不能仅用包含长时间idle的server吞吐日志下结论。

完成条件：

- [x] 真实server完成capture及实际decode replay，完整mempool数据/控制链路和安全释放
  有证据；正式模式无旧host回退，最终各rank的BM free=16。
- [x] 连续replay及请求复用通过，padding无真实slot访问，完成事件覆盖fetch/cache工作。
- [x] 用户curl小题目输出检查通过：关闭thinking后返回完整正确答案33，15 tokens、finish_reason=stop；详见下方2026-10-06记录。
- [x] 用户于2026-10-06确认当前性能无问题；长上下文容量独立跟进，完整指标表未回传。
- [x] 已记录用户S5验收及现有命令/日志/环境证据，解锁S6；部署SHA及完整测量资料仍待归档，不宣称已收齐。

完整精度数据集不属于S5；ticket04保持现有内容。完整压力/故障矩阵仍归后续票。

### S6. 全量code review、shadow删除与代码清理

**Blocked by:** 无阶段阻塞；S5已于2026-10-06按当前已验证容量范围通过用户验收。

**交付行为：** 对01–03引入的全部mempool代码及共享接入点完成review和整改，删除
shadow专用代码，形成清晰的正式/普通模式实现，并在最终清理版本重新取得硬件验收。
具体顺序和review清单见[S6计划](../ticket-03-s6-plan.md)。

2026-10-06已完成S6.1首次全量审查，报告见[review记录](../ticket-03-s6-review.md)。
S6.2已删除shadow/旧READBACK并修正Spec两项P2验证代码缺陷；S6.3已完成Standards三项
整改及增量复查。最终全范围复查和NPU复验待执行，以下总完成条件保持未勾选。
用户已确认STD-01共用row推导、STD-02分步查询/活跃视图优化，STD-03改为删除
fetch调试统计；两个Spec问题已随shadow/READBACK清理迁移，详见S6计划中的处理决定。

- 固定review起点和目标版本，覆盖完整开发增量；按仓库规范、spec与批准的阶段要求
  两条线审查，并检查职责、命名、可读性、重复逻辑及同步/释放边界。记录问题与整改，
  整改后再review，不能只审最后一笔cleanup diff。
- 全面删除shadow模式、双路径参考对照、专用readback配置/实现、诊断分支以及只服务
  shadow的脚本和测试。正式与普通路径仍需要的测试先迁移到相应入口，独立已知pattern
  的BM正确性gate继续保留；历史验收文档保留并标明历史用途。
- 删除无意义或重复检查，把静态配置/layout检查集中到适当的初始化边界；保留有
  正确性作用的peer契约、attempt/generation、binding、可读范围、容量和drain约束。
  不能为减少代码行数取消跨请求/跨进程边界的必要检查。
- 合并重复分支、状态与转换，改进变量/函数命名和注释；明确request row、P/D slot、
  token position及submitted/completed的不同含义，避免引入新的通用框架。
- 保留原非mempool传输、staging、host写入和host miss读取；同版本`MEMPOOL=0`
  实际恢复普通sparse PD，不依赖BM初始化或MemFabric运行环境。删除shadow不等于
  删除正式路径共用的BM writer、binding、Graph hooks或drain。
- 整理正式服务checker、启动脚本、测试及文档，证据覆盖全rank模式、实际分配、
  传输类别/字节数、Graph执行、完成与释放；删除生产路径selected/hit/P-miss/D-miss
  调试统计及checker依赖。取数路径的数值验证继续由独立fetch gate覆盖；保留必要的
  范围/覆盖/完成检查，去掉过期shadow操作说明。
- 在最终清理版本执行Mac适用检查、受影响组件gate、普通模式NPU回归，并重跑S5的
  真实server Graph、curl小题目和性能验收。S5旧版本结果不能替代整改后的实测。

完成条件：

- [ ] 全量review与整改后复查有记录，影响本票验收的问题均已解决。
- [ ] 全部shadow专用代码和入口删除，正式/普通模式的有效功能与回归覆盖保留。
- [ ] 冗余检查/代码、命名和可读性问题完成有依据的清理，必要正确性约束仍有验证。
- [ ] 同一最终版本通过普通模式回归和S5全部复验，包括用户curl检查及约定性能目标。
- [ ] 文档/脚本/证据与最终版本一致，用户确认实现与NPU验收后才关闭03。

## Acceptance criteria

S2独立gate已通过并获用户确认；以下为整票正式服务验收，仍须结合S3–S6的资源、
PD控制和真实attention执行结果核对，不以独立materialization gate替代。

- [ ] 复用02 backend runtime、P/D writer、统一 tick、准入和 drain，不重复开发真实 P
  offload；P native HBM cache 继续服务 chunked prefill。
- [ ] 将已验证的 P/D sparse fetch 接入 attention 输入；依据 prompt length/实际写入
  范围区分两个来源，保持 HBM sparse cache hit/miss/refill 和重置行为正确；
  始终校验binding、维护可读范围，padding/未写入位置不污染cache。
- [ ] 两个来源的 copy 在 Graph 中均存在，含 zero-valid 路径；attention 等待 copy
  和相关写入完成，不产生冲突 destination。
- [ ] 逐项确认 buffer 后关闭 main compact-KV transfer/staging；Index K/state/aux/meta
  管理和传输保留，D 联合 readiness gate 仍成立；不兼容的peer传输契约明确失败。
- [ ] 正式模式不分配重复长期 host KV，保留 SparseKVCacheManager 的 HBM sparse cache、
  materialization和reset职责；mempool runtime 不依赖该类的创建或生命周期。
- [ ] 用实际分配/注册/发送证据证明旧host KV、main-KV staging和main-KV traffic均为零；
  保留sparse设备路径与Index K容量计算，不能以关闭整个offload模式绕过检查。
- [ ] 旧main-KV传输、staging、host写入和sparse host读取代码均保留，按启动模式选择；
  同一版本关闭mempool后通过普通sparse PD回归，正式模式不发生隐式host回退。
- [ ] 保留 opt-in；普通与正式模式的短请求smoke、cutover数据内容及Graph由用户在NPU
  验证；用户用curl小题目核对输出，性能满足预先约定的目标。本票不加入正式数据集
  完整精度验收，04现有范围不变。
- [ ] 01–03全部mempool开发增量及共享接入点完成review和整改；shadow专用代码全部
  删除，检查、重复逻辑、命名及可读性完成清理，原非mempool实现保留。
- [ ] S6最终清理版本重跑S5全部验收及普通模式回归，不沿用清理前版本的通过结论。
- [ ] 按[阶段交付流程](../verification.md)核对实现、交付脚本并记录用户硬件验收。

## Verification

Mac覆盖S1–S6的行为合同及原模式回归；具体命令以实现后的测试入口为准。
用户运行小容量NPU gate，至少包含零decode、真实decode、连续请求和实际slot复用，
覆盖不同P/D slot、prompt边界、首个decode KV、HBM hit及两路miss，并验证Graph16。
有效KV内容由独立copy gate核对；正式服务日志证明attention取数、Index K/必要辅助
传输、旧存储/流量停用及全rank最终free=16。另验证同版本普通模式恢复旧链路。
HTTP 200或出现Graph日志本身不算通过。用户用curl发送一个小题目，记录预期答案、
实际输出和人工检查结论；该检查不外推为AIME26等正式数据集精度通过。
性能验收记录负载、预热/测量口径、TTFT、TPOT/ITL、吞吐及相关开销，交由用户
查看并判断。不要求预先提供阈值；未取得用户的性能确认时不能宣称S5完成。
S6记录全量review、整改和复查结果；最终版本再次运行S5及普通模式NPU回归。
ticket04内容不变；新数据来源导致输出异常时须在03定位，不能交付已知错误。

## Comments

### 2026-10-06：S6.3三个Standards整改

用户授权按删除统计、共用row推导、control查询/TP同步顺序实施，各部分分别验证和提交。
STD-03提交`25bfbddfa3`删除生产selected/hit/P-miss/D-miss及checker依赖，保留层覆盖、
非法读取、Graph完成和安全释放事实，新日志为`decode_completion`；27项针对性CPU测试通过。
STD-01提交`9ba89e46eb`共用纯`npu/kv_rows.py`，两条writer保留各自存储约束；
43项针对性CPU测试通过。STD-02为本记录所在的`refactor(ascend): bound control query and TP observation work`提交。

control改用完整identity直接查询和当前room owner接口；活跃协议与有界终态历史分开，
WAITING_RELEASE_ACK仍参与推进。TP仅同步逻辑协议及本轮清理事实，proof/session保留
本地原preflight验证。增量Spec复查发现的迟到P取消、各rank终态队列不同步两个回归已修复，
新增生命周期/事务回归覆盖旧消息、历史淘汰和空闲slot保护。Standards/Spec分别复查，
无未解决的增量发现。

preflight全量复制策略未改，单独测量显示4094终态加一个活跃请求时约11.5 ms，
查询约2.2 µs、TP观察458 bytes。完整方法、本地检查和复验说明见
[S6.3交付](../ticket-03-s6.3-summary.md)。这些是开发Mac数据，不是NPU性能结论。
最终独立CPU suite 200项、严格mypy 34源文件、standalone Ruff及生产hook规则、format、
isort和diff空白检查通过。实际NPU gate尚未执行，registered测试需要完整环境。
03保持open，等待最终全范围复查及用户执行NPU验收，不沿用旧版本S5结果。

### 2026-10-06：S6.2删除shadow/READBACK并修正SPEC-01/02

用户授权执行S6.2。先把两层BM取数、padding/复用、服务故障禁止释放及全rank日志证据
迁移到正式fetch/service测试，再删除shadow枚举、旧READBACK比较模块、runtime完成快照
及attention/service调用、全局配置和专用脚本/测试。SPEC-01资源gate不再期待S5拒绝，
SPEC-02启动测试分别期待正式P/D，保留普通模式与非法拓扑/容量拒绝。

日志checker迁移时，先用用例复现当前fetch非法选择报错会被漏检，再改为实际错误标记。
独立pattern的copy/writer/fetch gate保留；正式fetch调试统计本轮未删除，留STD-03/S6.3。
共享生产修改仅environ.py失效变量定义；外部glm51mempool.sh仅去掉一行export，格式不变。
旧操作说明移至archive，历史验收记录保持可追溯。

Mac完整独立CPU suite 192项通过，严格mypy 33个源文件通过；18个改动Python文件AST、
仓库Ruff规则/format及各目录isort配置通过，独立目录Ruff全检查通过；两份启动脚本bash
语法和formal/native四种dry-run通过。完整registered启动测试在Python3.9导入注解时失败，
未执行测试体；真实NPU资源、远端数值、Graph和模型输出尚未重测。

文件、审查、复验命令与证据边界见[S6.2交付](../ticket-03-s6.2-summary.md)。03保持open；
后续S6.3处理三个STD问题，最终版本再执行S6.5硬件验收，不沿用清理前S5通过结果。


### 2026-10-06：确认S6五项发现的处理方案

STD-01按共用纯NPU row/token推导整改。STD-02采用用户提出的四步：完整identity及
room owner的直接查询、活跃协议视图与历史留存分离、缩小TP决策同步并复用观察、
单独评估preflight复制成本。保留WAITING_RELEASE_ACK推进、迟到/重复消息校验及
预检后提交语义，不把结构性成本推断写成已经实测的性能退化。

STD-03按用户最新决定删除调试统计，不再进行指标命名整理；包括selected/hit/P miss/
D miss的计算、累计和报告接线，服务checker同步迁移。当前统计数组同时承载必要
层覆盖和非法读取检查，整改须拆分用途，保留正确性与完成/释放证据。后续统计绘图
需求另开测试类，不在此次清理中新建统计框架。独立fetch已知pattern数值gate保留。

SPEC-01移除verify_resources中的旧S5拒绝预期，让正式模式继续完成资源gate；
SPEC-02把registered启动测试迁移到最终正式/普通模式，随READBACK删除清理旧矩阵。
顺序为S6.2删shadow并修两个Spec问题，S6.3删统计、共用row、优化查询/同步，随后
S6.4复查与本地检查、S6.5同版本正式及普通模式NPU复验。

本轮更新方案与ticket记录，未修改生产代码或测试，未执行新的CPU/NPU验收。
五项均待实施；03保持open，NUMA/长上下文容量仍由用户另行处理，04范围不变。

### 2026-10-06：S6.1全量code review完成，待整改

用户授权“开始1全量code review”。固定起点4878a495d8、目标63590e9114，覆盖首次
独立实现至S5验收记录的30个提交、121个文件，以及上轮未提交的S6规划文档。
两路独立审查分别检查Standards与Spec，主审补充BM/runtime/Graph和共享资源接入。
完整范围、发现位置、影响、整改与验证方法见[报告](../ticket-03-s6-review.md)。

Standards未确认硬性规范违规，记录三项设计建议：row推导双份维护、service/control
查询与完整历史快照耦合、runtime fetch字段依赖数字列号。Spec确认两个P2：
verify_resources正式分支仍要求“S5拒绝”，两个mode用原helper直接复现AssertionError；
registered启动测试仍期待shadow，与当前正式mode及READBACK拒绝合同冲突。
未发现其他可证实的生产路径正确性缺陷，不把未测风险或既定shadow删除当新故障。

Mac实际执行独立CPU完整suite202项通过，严格mypy35文件通过，81个改动Python文件
AST及仓库hook指定Ruff规则通过，67文件format通过。默认Ruff额外43条未通过诊断单独
记录；完整SGLang registered suite和NPU未运行。已有S5用户验收仍按已验证容量有效，
不外推为整改后硬件通过。NUMA09继续封存，由用户独立处理。

本轮只交付审查与文档，五项发现均未修复。S6.2迁移覆盖/删shadow时处理两个验证
缺陷，S6.3处理设计整理，随后复查并执行最终正式/普通模式NPU gate。03保持open。

### 2026-10-06：S6执行规划细化

用户要求规划S6。已盘点shadow枚举、readback/runtime/attention/service调用及专用脚本，
明确共享environ.py删除旧环境变量的必要性。全量review起点固定为4878a495d8，
包含首个独立实现5a87606304，规划目标基线为63590e9114。
执行顺序为全量review→删除shadow并迁移覆盖→按职责清理→整改复查/本地检查→
最终版本正式和普通模式NPU复验；具体文件及交付见[S6计划](../ticket-03-s6-plan.md)。
保留现有KVArgs，NUMA容量归09用户独立处理，S6不新增正式数据集精度要求。
本轮仅规划，尚未实施或完成正式review；03保持open。

### 2026-10-06：用户确认当前性能，S5按已验证容量范围验收

用户确认“性能目前没有问题”；长上下文承载能力因NUMA相关问题仍待确认，
由用户另行解决设备驱动/硬件相关问题，不作为S5剩余阻塞项。结合已通过的
连续异步fetch gate、三请求全rank正式服务checker和关闭thinking后的正确完整回答，
记录S5在当前已验证的小容量配置下验收通过，解锁S6；本轮不启动S6实现。
性能结论来源为用户人工确认，未回传完整TTFT/TPOT/吞吐测量表，不补造数值，
不宣称16K或更长上下文容量、性能及完整数据集精度已获验证。
实际部署SHA、完整环境快照与性能原始数据仍是归档缺口，保留待补说明；
不将这些缺口写成已采集，也不重复要求已获用户确认的性能验收。
Ticket09继续封存，由用户独立跟进；Ticket03仍为open，待S6全量review、
shadow删除、清理及最终版本复验。本轮仅更新阶段文档，未运行NPU或修改生产代码。


### 2026-10-06：关闭thinking后curl小题目输出检查通过

用户沿用苹果题chat请求，仅增加`chat_template_kwargs={"enable_thinking": false}`，
模型GLM-5.1-w4a8，temperature=0、max_tokens=256、stream=false。
返回“17+25-9=33，现在有33个苹果。”，prompt_tokens=35、completion_tokens=15、
finish_reason=stop、matched_stop=154827，响应id=f76f25e630124d00a3e08c06ed7ec14d。
此前同题在thinking/起草内容中达到256-token上限；本次算术及一句话格式正确且无截断。
记录S5小题目输出检查通过，不等同于正式数据集精度通过。完整输入及响应字段见
[S5总结](../ticket-03-s5-summary.md)。证据来自用户粘贴响应，agent未执行NPU请求。
性能确认、部署SHA及最终环境记录仍待补齐；03保持open，S6未开始。
本轮仅更新三份验收文档，执行限定文件的`git diff --check`。

### 2026-10-06：S5三请求正式服务checker通过，回答与性能仍待验收

用户按已交付HTTP命令顺序运行zero/decode/reuse三个请求，实际输出token数分别为
1/32/32，finish_reason均为length。输入是`/generate`原始文本，temperature0、
ignore_eos=true；保存于`/tmp/ticket03-s5-requests/`的请求与响应文件。
在P机`/home/cryang/sglang`执行：

```bash
python3 ascend-mempool-test/scripts/verify_service.py \
  --prefill-log ../p.log --decode-log ../d.log \
  --requests 3 --layers 78 \
  --report /tmp/ticket03-s5-formal/service-result.json
```

用户回传`FORMAL_SERVICE_PASSED`及完整JSON：requests3、ranks_per_side16、
zero_decode_requests1、decode_requests2。全32条资源记录均为正式模式，旧host KV、
staging、main-KV注册为0，transport_staging=false；P保留native HBM KV，D保留sparse
cache，两侧均注册78个Index K条目。每rank三请求累计Index K发送7,667,712字节、
aux4,800字节。报告结合checker合同证明全rankmapping/capture/replay、联合readiness、
fetch完成、P/D slot新generation复用、释放顺序及最终free16检查通过。

32-token输出包含截断的天空解释，以及广告/多语言的冰浮水文本；本轮强制长度的
原始文本续写不作为回答正确性验收。不能仅凭这些输出判定KV污染，也不能以checker
通过排除数值/模型输出问题。下一步用chat接口的完整小题目回答核对，异常若复现，
在S5定位，不交给S6或后续精度票。

详细逐rank资源口径及证据边界见[S5总结](../ticket-03-s5-summary.md)。报告仍明确
accuracy/performance为pending；部署SHA、依赖版本未另行提供，agent未直接读取
远端原始日志。本轮更新已通过的S5执行/资源/复用检查项，保留输出和性能项未完成，
03仍open、S6未开始，09仍封存。仅记录用户验收证据，未修改生产代码或重跑NPU。

### 2026-10-06：S5连续异步fetch组件NPU gate通过

用户回传P=`10.120.72.31`、D=`10.120.72.32`于11:55:26–30运行
`verify_fetch.py`的完整终端输出；两侧均使用device0，S_P=8、S_D=16、2层、1head、
dim576、graph_rows16、active_rows3、topk2048、block_dims24/48、replay_cycles2、warmup3。
命令与[正式服务验收说明](../../../ascend-mempool-test/FORMAL_SERVICE.md)第1节一致。

两侧均输出`ALL_CHECKS_PASSED`。D共30个`FETCH_PASS`，包含10个eager和20个replay，
每项`queued_forwards=5`、`verified_elements=37748736`。每组连续提交五个case对应的
forward后才统一同步检查，覆盖P miss、D miss、mixed、all-hit、zero-valid及padding；
逐层copy计数分别为[6,0]、[0,6]、[6,6]、[0,0]、[0,0]。成功标记还要求脚本中的
未完成detach拒绝、完成报告核对、重绑和pool/SDK清理均无异常。

D初次GVA转VA查询失败后已出现完整MAPPED及后续所有数值检查通过；本轮未形成持续
映射故障。日志中的可选扩展库、tag/key、store响应和base-format提示未阻断本次gate。
两侧报告为`/tmp/ticket03-s5-fetch-p.json`和`/tmp/ticket03-s5-fetch-d.json`，
日志分别为同目录`ticket03-s5-fetch-p.log`和`ticket03-s5-fetch-d.log`；原始文件未由
agent读取，实际部署
Git SHA及本轮依赖版本未另行提供，不以本地HEAD代替硬件部署版本。

记录本项组件gate通过；不重复要求此前S2测试，也不据此关闭S5或03。下一步为真实
服务zero-decode、连续decode与新generation slot复用及完整日志checker，再完成小题目
完整回答和用户性能确认。S6未开始，09保持封存。本轮仅更新验收文档，未运行本地NPU。

### 2026-10-05：用户回传首个S5真实服务请求，生命周期完成

P=`10.120.72.31`、D=`10.120.72.32`，用户经6699 router向chat接口发送铅笔题，
temperature0、max_tokens512；HTTP 200，prompt47、completion512。
已交付SGLang `fadc8223cd`和启动脚本`24a6857`，实际部署SHA未随反馈提供。
room=`6498740039664540360`，attempt=`506bc05162d7381522936c1da6c5d2b6`。
用户粘贴日志中全16 ranks的联合readiness、512次真实Graph replay、完成drain及
detach/free/DONE/ACK顺序正常，P/D最终free16。每rank仅发送Index K 2,555,904字节
和aux 1,600字节，main_kv_bytes0。P native先释放，P BM持有到D完成。

全部D ranks报告`status=completed`、`drained=true`、`cancelled=false`，
`forwards=replay_forwards=submitted_kv=written_kv=512`，78层共39,936次layer_checks。
P miss3,666、D miss39,936、cache hit12,076,974、selected12,120,576，计数等式成立。
回答中的算术结果6正确，但finish_reason为length，最终答复截断，尚未记录输出验收。
稳定server滚动吞吐6.92–6.97 token/s；没有准确客户端TTFT/TPOT及用户性能确认。
此前18:15:53的400没有错误响应body，原因未确定；随后此请求正常完成。

本轮仅记录用户证据并核对本地计数/输出处理代码，没有重跑NPU或修改生产代码。
详细时间线、curl输入、计数、证据范围及后续补测见[S5总结](../ticket-03-s5-summary.md)。
完整回答、zero-decode/连续请求/slot复用及启动日志checker、连续五步异步组件gate、
性能确认仍待补齐；03保持open，S6未开始，ticket04不变。

### 2026-10-05：S5实现交付，等待用户执行NPU验收

按用户授权接通正式P/D启动模式，提前拒绝正式READBACK组合；增加backend/runtime
模式一致性核对、完成事件后fetch统计、实际资源及Index K/aux发送证据。
生产修改为5个Ascend/NPU文件；复用既有copy、attention、Graph和whole-D drain入口。
新增正式P/D启动脚本、日志检查器和可直接执行的服务/性能验收说明；组件gate改为
输入预先上设备、连续5个forward提交及设备快照、最后统一同步收集。

Mac独立CPU完整suite 195项通过，mypy 34个源文件通过。
规范评审2处文档状态已修正，无阻塞项；规格评审1项P2为P侧日志漏检，已用5个失败
subtest复现并修复，checker定向6项通过，评审复核无遗留规格问题。
代码路径、检查和阶段评审见[S5总结](../ticket-03-s5-summary.md)，用户运行命令见
[正式服务验收说明](../../../ascend-mempool-test/FORMAL_SERVICE.md)。
尚未运行NPU组件/真实server Graph、用户curl小题目及性能测试。
用户最新决定：验收时自行查看实测TTFT、TPOT和输出吞吐并判断，不要求预先提供阈值；
普通sparse PD保留作诊断对照，实际性能结论待用户确认。
03保持open，硬件验收项未勾选；S6未实施，ticket04未修改。

### 2026-10-05：按用户确认更新S5/S6验收与清理范围

用户要求S5在真实SGLang server和NPU Graph replay下验收完整mempool链路与预期
性能；输出检查简化为用户用curl发送一个小题目并核对回答，不增加AIME26等正式
数据集的完整精度验收。性能数值目标仍待验收前明确，ticket04不修改。
S6增加覆盖01–03全增量的code review、全部shadow专用代码删除、冗余检查/代码清理
和命名/可读性整改；保留普通模式及正式路径共用功能。清理后的最终版本须重跑S5。

本轮更新票面、S5计划，新增S6计划并同步README入口；只修改计划文档，S5/S6仍待
实施及用户NPU验收，03保持open。本轮未运行行为测试或NPU测试。
文档检查通过：6份Markdown的57个本地链接、代码围栏及行尾空白核对；
`git diff --check`通过。ticket04文件SHA-256前后一致；未add、commit或push。

### 2026-10-05：用户确认 S4 NPU gate 通过，进入 S5 规划

交付并推送的S4提交为`8e00b36cf980159f9228bdd1c3440adc0c51f4a4`。
用户在P `npu1-31` / `10.120.72.31`及D `npu1-32` / `10.120.72.32`执行
`verify_pd_transfer.py`，两端均使用device0、store port18875、control port18876、
timeout600；运行命令带仓库`python`目录的PYTHONPATH。完整命令见
[S4 gate说明](../../../ascend-mempool-test/PD_TRANSFER.md)，head/local IP按上述机器填写。
报告与日志路径为`/tmp/ticket03-s4-{p,d}.{json,log}`。

已核对用户粘贴的双端控制台：`bm_first`、`transfer_first`、`empty_last`、
`bad_layout`、`aux_failure`、`cancel_inflight`均输出PASS；字节数依次为
198176、198176、1568、0、196608、198176，两端一致，均以`ALL_CHECKS_PASSED`
结束并返回shell。`bad_layout`故意把目标层号从[1,4,7]改为[0,1,2]；P端拒绝日志和
后续Session failed是预期负向验证，发送0字节并完成失败收尾。
用户随后明确确认“S4完成”，据此将S4阶段记为已验收，解锁S5规划与后续实施入口。

本轮未独立读取远端JSON/log文件，也未获得机器实际HEAD、环境版本或显式退出码。
以上版本是已交付提交，以上结果是用户回传并确认的证据。该gate使用真实NPU
Index K/aux传输及原worker，BM readiness仍由fixture提供；不外推为正式服务、
真实BM fetch、完整TP16 collective或模型精度验收。Ticket03保持open。

核对当前config/runtime/copy、NPU Graph、attention和service释放入口后，新增
[S5计划](../ticket-03-s5-plan.md)。当前MEMPOOL开关仍选shadow，正式服务保护仍在；
S5拟补连续异步replay、完成事件、row/slot复用和实际forward容量验证，再开放正式入口。
本轮为验收记录与计划同步，未实施S5生产代码。
文档检查通过：5份Markdown的49个本地链接、8个完整拟改代码路径及代码围栏核对；
`git diff --check`通过。S5解释页渲染为7个面板，STE检查0条警告。
未运行新的行为测试或NPU测试；未add、commit或push。

### 2026-10-05：S4 组件实现与验证脚本交付

按用户最终方案，仅修改5个现有Ascend/NPU生产文件。正式P/D只发布BF16 Index K，
P native HBM K/V仍保留用于计算；原KVArgs、utils、factory、scheduler和共享worker不改。
service在物理注册后、receiver发布前补齐真实层号与组数，加入peer模式/layout/native
session匹配，payload升级到v2并保留原路由tag。正式发送检查失败返回码交给原worker；
空末chunk的aux/state入口同样受检查保护。普通/shadow继续原路径，服务启动保护推进到S5。

交付[S4总结](../ticket-03-s4-summary.md)与[双机gate说明](../../../ascend-mempool-test/PD_TRANSFER.md)。
新增gate使用真实NPU Index K、完整MetadataBuffers、TransferEngine和原sender/worker，
覆盖两种readiness顺序、metadata延迟、非连续页、多chunk、零页末chunk、aux失败与取消。
控制测试提供BM写完事实，不执行BM数据、TP collective或完整scheduler；这些仍需后续门禁。

agent在Mac实际执行：独立CPU suite **190 tests通过**；严格mypy覆盖独立src/scripts、
BM runtime、协议/service/config，共**31个文件通过**；按独立测试配置进行Ruff检查、
52文件格式检查、isort与git diff --check通过。规范评审无硬性违反；需求评审发现的
gate八层容量错误已修复并复核通过，新增CPU回归覆盖。完整评审记录见S4总结。

**NPU尚未执行，等待用户执行NPU验收。** Ticket03保持open，S4硬件通过与实现核对后
才推进依赖的S5；本次不宣称正式模型服务或主线KV流量已获硬件验证。

### 2026-10-05：按用户决定保留原 KVArgs，在 Ascend 发送入口按模式分流

用户明确认为 AscendKVArgs 没有必要，要求直接使用现有 KVArgs，不创建
`disaggregation/ascend/args.py`，并把模式与发送内容的判断放到发送端。
据此重写[S4方案](../ticket-03-s4-plan.md)：取消子类、工厂分派、动态扩展 KVArgs
以及单独的 mempool_transfer 模块；预计只修改 5 个现有 Ascend/NPU 生产文件和 utils。

实际复制入口为 `AscendKVManager.send_kvcache()`；`AscendKVSender.send()` 继续
承担原有页数累计、末 chunk 判断与入队。模式从实际 pool 经现有 service/PoolPeer/control
提供。注册先于发送，因此保留 NPU buffer 发布时过滤 main K/V 的必要步骤，避免仅关闭
流量却保留重复注册；utils 仅在已有 NPU 分支填写原字段中的实际层号与组件组数。
本轮仅修订方案；S4 生产代码、NPU gate 和正式服务验收尚未实施。
Mac 文档检查通过：`git diff --check`、两份文档 26 个本地链接与代码围栏检查、
6 个拟修改生产路径存在性检查；更新后的 HTML 渲染通过且 STE 0 条警告。

### 2026-10-05：复核 D 项，撤回共享 worker 校验 hook

用户指出 Index K 原有传输链路继续复用，询问为什么需要在 worker 增加校验。
核对后区分两件事：S4 过滤 main K/V 会改变发布/注册条目，需检查新布局及 peer
兼容性；原始页索引裁剪和零页 chunk 则是已有 worker 行为，尚无证据表明本次过滤
会新增相关缺陷。上一版将这类通用防御检查作为主线修改理由，范围偏大。

修订[S4方案](../ticket-03-s4-plan.md)：保留 Ascend 本地注册、握手及现有
`send_kvcache()` 正式分支中的布局检查；完整 registration 可从继承的 manager
注册表取得。删除拟议的 `_validate_transfer_target()` 主线 hook，不新增通用
页索引等长检查，不改原 worker 和 aux/drain 流程。生产主线范围从两文件缩为 utils。
这是 agent 在方案讨论中的修正，不记为用户已确认实现；S4 仍待实施与 NPU 验收。

### 2026-10-05：按用户要求收敛 S4 的生产代码范围

用户要求“尽量只动 ascend 或 npu 目录下代码；如果一定动主线，说明必要性”。
重新核对工厂、NPU 参数组装、manager 注册时机及 worker 截短/空 chunk 分支，
重写[S4方案](../ticket-03-s4-plan.md)：以 AscendKVArgs 子类承载描述，局部 helper
消费实际 pool 的 mode/层号；主线 utils 仅分派和接入，Mooncake worker 仅增加可覆盖
校验方法并沿用失败/drain 收尾。取消上一版对公共 KVArgs 与 P/D 初始化文件的拟议修改。
该方案以两处主线文件的窄接口复用既有控制流，不新增全局 pool 注册表或复制 worker。
本轮为方案修订，未实施 S4 生产代码或执行 NPU；Ticket03 继续 open。

### 2026-10-05：用户确认 S3 gate 通过，讨论 S4 实现方案

用户明确反馈“S3 gate通过”，据此记录 S3 NPU 资源 gate 已验收。
S3 交付版本为 `5b18a8046c085fda24e115bd2251ecf19b0898b7`，先前交付的命令在
仓库根目录按五种 mode 分别运行 `ascend-mempool-test/scripts/verify_resources.py`。
命令要求完整 SGLang / Python 3.11 / torch_npu / sgl_kernel_npu / CANN 环境及空闲 NPU；
预期证据位置为 `/tmp/ticket03-s3-version.txt`、`/tmp/ticket03-s3-<mode>.json` 和对应 log。
本次仅收到用户通过确认，未附实际机器 HEAD、主机/device、环境版本或报告内容；
不将这些待补信息写成已核对事实。资源 gate 不覆盖真实跨机 Index K/aux 或正式模型服务。

本轮核对实际 buffer 发布、KVArgs、worker、mempool peer 及 handoff 入口，形成
[S4 代码修改方案](../ticket-03-s4-plan.md)：正式模式只发布/发送 Index K 及必要辅助数据，
增加完整传输描述与发送前校验，保留联合准入和 native/BM 分开释放。
S4 尚未实施；本轮仅同步验收和规划文档。S1 完整环境回归分别跟踪，S4–S6 待完成，
Ticket03 保持 open，整票验收项不提前勾选。

### 2026-10-04：S3代码交付，等待用户执行NPU资源验收

按用户授权先提交S2 gate修正及验收记录为`dd1f92f618`，再实施S3。
正式D按mode跳过旧host SHM/mapping/length tensor及staging copy stream；保留cache、
Index K和必要stream/event。attention跳过旧host写入，BM writer/fetch沿用已有实现。
host/staging入口有明确模式保护，普通/shadow路径保留；request init/alloc/free/clear
在host metadata缺席时可执行，row复用清map，续跑chunk不清map。

资源构造器允许独立验证正式模式。ModelRunner/backend/runtime工厂仍拒绝正式服务启动；
native pool的PD发布入口同样保留保护，提示S4–S5。当前env开关仍选择shadow。
本次不提前修改S4传输契约或把main-KV traffic为零记为通过。

Mac独立CPU suite **176项通过**；mypy检查配置/BM/独立脚本 **27个源文件通过**；
10个改动Python文件的Ruff lint/format与新文件isort通过。新增测试覆盖实际sparse构造、
旧host入口拒绝、普通host读写、staging→host、实际PD adapter索引路由/Success分支与
无host的请求复用。CPU替换SDK/serving依赖边界，不当作完整构造链/NPU证据。
registered配置测试在本机Python3.9导入`str | None`时失败，未执行；完整环境回归待执行。

规范审查无发现。规格审查发现gate的staging seed与copy stream缺少生产者同步，
已在注入Success前同步NPU并通过复查；无剩余规格问题。修正后14项定向CPU回归、
脚本Ruff lint/format与mypy检查通过，真实NPU同步仍待下述gate验证。

新增`ascend-mempool-test/scripts/verify_resources.py`，在用户NPU环境按五种mode分别
运行真实pool/allocator/Index K、资源计数和普通host copy回归。完整命令、预期资源矩阵、
失败判据和待回传文件见[S3交付总结](../ticket-03-s3-summary.md)及独立测试README。
PD父transport使用recorder，不进行跨机传输；S3资源gate和正式服务S4–S6均尚待硬件验证。
Ticket03保持open，验收项不提前勾选。

### 2026-10-04：S2独立NPU gate重测通过，用户确认

用户在P `npu1-31` / D `npu1-32`的device0重跑`verify_fetch.py`，将K改为2048，
其余仍为P/D容量8/16、2层、Graph width16、3个真实rows、block_dim24/48、
warmup3和replay2。两端均输出`ALL_CHECKS_PASSED`并返回shell。
D共30条`FETCH_PASS`，覆盖2个block_dim × 3轮 × 5个case；每层P/D copy计数为
p_miss `[6,0]`、d_miss `[0,6]`、mixed `[6,6]`、all_hit及zero_valid `[0,0]`。
逐元素比较含全部padding，当前D epoch从-1到-6均通过；同一Graph复放、目标A/B
切换及P/D slot更换均在此矩阵中覆盖。独立gate正常完成drain和双侧释放。

P启动时的对端GVA转换失败随后完成映射重试、peer probe及实际读取，未阻断gate。
用户明确表示“我认为没有问题了”，据此记录S2实现核对及独立NPU gate完成。
详细环境、输入、计数和证据边界见[S2总结](../ticket-03-s2-summary.md)。
交付基线为`9036be2b0f`；机器实际SHA、环境报告和JSON内容未随消息提供。
用户实际日志/报告路径仍为`/tmp/ticket03-s2-{p,d}.{log,json}`，以本次控制台日志
及用户确认作为已核对证据，不声称独立读取过远端报告。

本轮只同步文档，未运行新的代码测试、未add/commit/push，也未实施S3。
下一开发步骤为S3；S1完整环境回归仍待补齐，S3–S6和正式服务验收尚未完成，
03保持open，整票验收项及依赖票状态保持不变。

### 2026-10-04：首次S2 NPU gate失败，修正lookup宽度并等待重测

用户回传双机gate日志，参数为P/D容量8/16、2层、Graph width16、3个真实rows、
K=8、block_dim24/48和2次replay。D在warmup调用真实`slot_map_lookup`时抛出
`requires topk=2048, got 8`，P收到D失败结果，`checks=0`。本轮未完成fetch校验。
初版交付commit为`9036be2b0f`；日志未附机器实际SHA，重测时一并记录。

算子源码固定K=2048，而初版gate命令为8、共享parser默认为64；CPU lookup替身
没有暴露这一约束。fetch入口现默认2048，并在BM连接/分配前拒绝其他宽度；
原copy-only gate保留可变K。P/D容量8/16仍适用，少量有效位置之外用-1补齐。
新增CLI回归先复现失败；修正后CLI/materialization定向10项通过，mypy26个源文件
及格式/lint检查通过。生产fetch/runtime/kernel未改动。

更新后的双机命令和故障记录见[S2总结](../ticket-03-s2-summary.md)，重测使用
`ticket03-s2-k2048-{p,d}.{log,json}`保留首次失败文件。S2硬件尚未通过，03仍为open，
不勾选验收项或解锁依赖；S3–S6仍未实施。

### 2026-10-04：用户确认S2代码并授权提交

用户核对了materialization、KVFetch、runtime及Graph固定输入设计后，明确授权提交
本次S2代码、测试和说明。随后先讨论S3/S4/S5，尚未授权实施这些步骤。
本次提交前同步交付状态并检查diff；沿用此前已通过的165项CPU测试及静态检查结果。
尚无新的NPU结果，正式服务启动保护及Ticket03的open状态保持不变。

### 2026-10-04：S2实现交付，等待用户代码核对和NPU验证

用户明确授权实施S2，并要求结束后不要add。S1当前基线为`3d2f3c6168`；本轮修改
保留工作区，未add/commit/push。S1完整环境/NPU待验收记录仍保留，继续实施授权
不作为硬件验收证据。

正式decode新增`KVFetch`，复用两次UniDexCopy，以独立P/D binding和可读前缀只填充
selected KV的miss位置；沿用HBM lookup/hit/refill/map及现有stream/event。
attention传入padded top-k并使用公共valid mask。READBACK关闭时仍强制D的P slot
校验，维护实际forward进度，完成事件之后核对coverage和非法正索引，错误进入fault。
正式fetch和shadow readback不可同时启用，普通/shadow host分支保留。

审查后补充Graph同形状目标更换时固定metadata的持有，以及设备实际位置与host
expected prefix一致性检查；读取上界同时受本层实际write counts约束。新gate覆盖
capture(A)→eager(B)→replay(A)，D数据带每轮独立标记避免预填旧值掩盖漏写。
Mac最终完整独立CPU suite 165项通过；两项审查发现均先用测试复现，再修复，相关4个
测试文件共38项定向复测通过。mypy 26个源文件通过；规范审查0项硬性违反、1项非
阻塞接口建议，规格审查2项P1已修复并复核，无遗留阻塞项。检查命令、最终复核结果及
完整双机NPU命令见[S2总结](../ticket-03-s2-summary.md)。

正式模式启动保护保留，错误提示改为S3–S5待完成；MEMPOOL环境开关仍启动02 shadow。
S2 gate用测试fixture仅分配materialization资源，不代表生产分配/PD切换已通过。
尚未运行NPU、完整SGLang服务或模型精度验证，03保持open，验收项不勾选，依赖票未解锁。

### 2026-10-03：S1实现交付，等待完整环境与NPU回归

用户授权仅实施S1，并要求不执行git add。本轮新增明确的P/D shadow模式及预留正式
模式，拆开sparse HBM cache、host KV、D staging、BM四项能力。已有mempool开关
仍选择shadow；正式模式在资源分配前明确报S2–S5未接通，READBACK不参与模式选择。
runner在KV容量计算/分配前解析mode并复用MempoolConfig校验；同一mode传给
configurator、pool、backend、sparse manager，BM runtime仍在backend之后、Graph之前
创建。普通P保留native KV，普通/shadow D保留Index K与原host/staging链路。

代码路径、初始化合同、检查命令与逐机复测步骤见[S1总结](../ticket-03-s1-summary.md)。
Mac实际执行：独立CPU suite 151项通过，mypy 24个源文件通过，Ruff、isort及语法/
diff检查通过；规范/规格两路静态审查各0项发现。完整registered配置测试尝试运行，
被Mac Python3.9与现有SGLang `environ.py` 的`str | None`注解不兼容阻断；新增真实
pool构造/启动测试及HiSparse兼容测试不计为本地通过，交付在完整环境中补跑。

等待用户执行NPU验收：02小容量shadow回归、READBACK关闭时的同模式回归，以及
同版本MEMPOOL=0的普通sparse PD回归。本轮未执行NPU，未关闭03或解锁依赖票；
S2正式fetch、S3停用host/staging、S4传输切换及S5/S6仍未实现。未执行add/commit/push。

### 2026-10-03：旧路径关闭而不删除

用户明确要求保留原SGLang KV传输及D sparse attention从host SHM读取的实现。
补充S1启动模式/同版本恢复合同和逐入口接线设计；S2只替换miss来源，S3/S4按模式
跳过旧分配、host读写、main-KV发送与staging处理，保留共同HBM cache与PD控制。
S6增加普通/正式两种模式的实际路径回归，普通模式恢复不依赖02历史提交或shadow。
同步修正spec/design中容易理解成删除旧实现的表述。本次仅更新设计，未修改生产
代码或运行NPU；03保持open，所有验收项未勾选。

### 2026-10-03：核对P端保留native KV、移除compact-KV传输的控制影响

用户指出正式P需要保留native HBM page cache，并要求核对停掉原KV传输是否影响PD控制。
核对bootstrap、sender计数、transfer worker、P inflight、D metadata gate及mempool释放后，
确认应保留Index K/state/aux的真实handoff与完成通知，单独过滤compact-KV buffer。
补充S1“分配与传输独立”约束及S4控制入口、完成/释放时序和待实现的集成验证。
同时明确当前BM池物理存储为DRAM，非HBM池。

本轮实际执行：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/tests/unit python3 -B -m unittest test_pd_tick.TestMempoolTPTick.test_handoff_keeps_p_slot_until_uniform_drain_and_ack -v
```

结果1项通过，覆盖16对rank的联合readiness和drain/ACK后归还slot。
另对生产`CommonKVSender._prepare_send_indices()`及`should_send_kv_chunk()`做CPU抽取
执行：总page数为2，依次提供1页、1页时正确识别末chunk；直接清空indices时，即使
scheduler允许最后空chunk发送，也不会满足sender的末chunk计数。该检查未运行传输
worker或NPU，不证明03的Index K-only数据路径已接通。本轮只更新规划，03保持open。

### 2026-10-03：用户确认六步安排，进入S1讨论

用户要求按上述六步更新ticket03，并讨论S1“拆分配置与资源职责”。新增步骤进度表，
细化S1的现有三处配置耦合、建议mode/能力矩阵、资源归属、初始化顺序和CPU完成条件。
建议复用现有enum，增加正式P/D模式；内部能力由mode派生，readback不参与资源策略。
这些具体接口建议及同版本shadow偏好尚待讨论，不记为用户已确认的实现方案。
本轮只修改规划文档，未修改生产代码或运行代码测试，03保持open、所有验收项未勾选。
文档检查通过：`git diff --check`，两份文档18个本地链接、代码块闭合及六步/状态检查。

### 2026-10-03：细化03实施规划，尚未实现

用户要求先规划本票。核对当前attention/cache、runtime、NPU MLA pool和Ascend PD
buffer路径后，将工作整理为S1–S6，补充资源清单、完成条件和验证证据。
关键接线点：readback条件下的prompt slot/可读长度管理须独立；host开关目前同时影响
sparse设备路径/容量计算；Index K实际位于kv_data_ptrs尾部；hit/refill与miss须使用一致
的binding和written-range有效性规则。同版本是否继续提供shadow模式保留为待选偏好。
本轮仅更新规划，所有03验收项保持未勾选；没有执行03代码或NPU验收。

### 2026-10-03：02关闭，本票成为下一执行入口

用户确认02真实KV读回、Graph、正常释放和物理slot复用验证无问题。
本票的02依赖已满足，保留blocking link作为依赖历史；State仍为open。
本次仅更新交接说明，正式attention数据来源和旧hostSHM/main-KV移除尚未实现，
不将02的shadow数值通过视为03的cutover验收。

2026-09-30：按用户确认更新票面，原真实 P offload 已归02 shadow；本票改为正式
数据路径 cutover，04负责正式集成验收。文件名保留以兼容现有链接。未实现/未验收。
