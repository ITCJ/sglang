# 03: 正式 mempool 数据路径 cutover

**What to build:** 在02已验收的真实 shadow 双写/控制/readback 基础上，让 D attention
实际消费 mempool sparse fetch 的 KV；按模式停用原 main compact-KV transfer、staging
和长期 host KV allocation。旧实现全部保留，关闭mempool后同一版本可恢复原sparse PD
路径。保留 P native HBM cache、HBM Index K 及必要辅助传输。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [02: 16 对 rank 的正常控制闭环](02-rank-pair-control-lifecycle.md).

**Status:** ready-for-agent

**State:** open

## 规划状态（2026-10-04）

02已获用户确认验收并关闭，已有链路、代码入口及证据见[02总结](../ticket-02-summary.md)。
用户已确认按S1–S6组织本票，并先后授权实施S1和S2。S1代码已提交为`3d2f3c6168`，轻量CPU检查通过，
完整SGLang环境的资源/启动单测及NPU回归待执行；见[S1交付总结](../ticket-03-s1-summary.md)。
S2代码和独立NPU gate已提交为`9036be2b0f`；首次K=8参数错误修正为2048后，
双机30个case及正常退出均通过，用户于2026-10-04确认S2独立NPU gate无问题；见
[S2交付总结](../ticket-03-s2-summary.md)。用户随后授权先提交当前修改，再实施S3；S2修正与
验收记录已提交为`dd1f92f618`。S3代码与Mac检查完成，NPU资源gate待用户执行，见
[S3交付总结](../ticket-03-s3-summary.md)。S4–S6仍待实施，不表示正式服务数据路径或本票验收已完成。旧KV传输、D staging/host
写入及sparse attention的host SHM读取均关闭而不删除。S1用显式shadow模式保留02
链路，正式模式暂拒绝启动；03完整交付后是否另保留shadow诊断模式仍是独立待定事项。
继续使用context1024、P/D各512、TP16、D Graph width16、NUMA `0,2,4,6`的小容量配置。
大容量/NUMA调查仍归延期的[09](09-numa-allocation-followup.md)，不阻塞本票。

本票交付完整的正式数据路径及小容量smoke；[04](04-single-request-graph-decode.md)
负责正式Graph/模型集成与固定greedy baseline对照，05–08继续承担并发、容量/取消、
故障和AIME26验收。共享路径保留原非mempool模式。

### 启动模式建议

建议现有 `SGLANG_NPU_ENABLE_MEMPOOL=1` 在03交付后选择正式路径，02的shadow运行
保留在已验收版本。用户也可选择同一版本继续支持shadow/正式两种模式；该偏好已单独
询问，尚未作为新增配置或协议字段实施。下面各项在两种选择下都需要完成。

关闭 `SGLANG_NPU_ENABLE_MEMPOOL` 后，同一版本必须恢复原非mempool路径；启用
sparse offload时，P/D分别选择原 `PD_PREFILL_NATIVE` / `PD_DECODE_OFFLOAD`。
恢复普通路径不需要切回02提交，也不依赖保留shadow双写模式。

正式路径的正确性不依赖 `SGLANG_NPU_MEMPOOL_READBACK`。原readback以旧host路径作为
独立参考；停用该参考路径后，不能继续宣称同样的逐元素校验通过，更不能将BM结果与自身
比较。实现时同步确定此开关在正式模式的适用范围与不支持组合的明确报错，并更新脚本。

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
| S3 按模式停用重复存储 | 旧实现保留；正式模式host KV和main-KV staging分配为零 | 代码已实现；Mac检查通过，等待用户执行NPU资源gate |
| S4 精简PD传输 | 仅保留Index K和必要辅助数据，保留联合readiness | 待实施 |
| S5 核对Graph与生命周期 | 固定地址、正确stream依赖、安全drain与释放 | 待实施 |
| S6 测试与交付 | CPU验证、正式服务checker、用户NPU验收 | 待实施 |

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
同版本是否还支持shadow仍是上文待定偏好；若选择保留，须明确表示shadow模式及
peer契约，不得让 `READBACK` 暗中决定attention的数据来源或是否分配旧host KV。

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

### S5. 核对Graph、stream依赖和生命周期

- Graph保留P/D两个copy调用，包括某一来源zero-valid和padded rows；固定metadata
  地址，按请求更新slot、prompt length、decode written length、indices和mask。
- stream顺序为本层BM write/metadata更新→miss copy；attention等待hit和两路miss；
  refill等待selected KV就绪；下次使用cache前完成refill及slot-map更新。
  将这些事件纳入runtime completion和现有whole-D drain覆盖范围。
- 复用02的row detach/native free、D release/DONE、P release/ACK次序。P原handoff
  仍有Index K等访问，不能因main KV不再传输而把KV_READY当作native回收许可。
- 保留对尚未写入位置、stale binding和未完成工作的检查；逐forward证据在完成事件后
  汇总，Graph replay不依赖capture时的Python请求常量或host同步。
- 按实际forward语义核对容量：P已采样首token，overlap可能有额外forward，已验收
  32-token用例实际写入32个D KV。测试P/D容量、总context/Index K范围和真实提交上界，
  不固定假设总是N-1或N+1；完整容量压力矩阵留给06。

完成条件：同一Graph可处理不同P/D slot及后续请求，padding不访问真实slot；D drain
覆盖新增fetch/cache工作；正常结束和零decode均能完整释放。压力/故障矩阵仍归后续票。

### S6. 测试、可观察证据与交付

- CPU测试从实际module入口验证配置/分配选择、P/D路由、cache hit/miss/refill/reset、
  关闭readback后的正式读取范围、Index K列表/页索引、联合readiness及原模式回归。
  覆盖短top-k与batch>1、row 0/padding以及copy对象的目标buffer更换。
  对被关闭的旧路径使用会报错的测试替身，捕捉隐藏调用或回退。
- 同一版本覆盖两个完整模式：正式模式旧SHM/staging分配和host读写调用为零，
  main compact-KV发送字节为零，BM miss及cache hit/refill有效；`MEMPOOL=0`时恢复
  旧资源分配、main-KV传输、staging→host、decode host写入及host miss读取。
  普通模式的测试不得以mock新BM路径代替原路径成功执行，且不要求MemFabric依赖。
  用户NPU交付至少各跑一轮普通sparse PD与正式mempool短请求smoke，使用各自容量配置；
  同时记录实际allocation、发送类别、取数来源和请求完成，不能只测试enum布尔值。
- 复用01/02独立BM writer/copy gate，以已知KV内容验证正式fetch及其目标buffer。
  有独立旧路径参考时才做shadow数值对照；最终正式服务不把来源计数当作数值比对。
- 增加正式服务验收入口或扩展现有checker，明确区分02 shadow结果与03 cutover结果。
  记录全rank启动模式、host/staging分配、传输buffer类别/字节数、hit/P-miss/D-miss、
  Graph实际forward和release结果；不能沿用 `SHADOW_READBACK_PASSED` 冒充正式验收。
- 更新 `ascend-sglang-script/pd-disaggregation/glm51mempool.sh` 和测试说明，交付
  P/D启动、请求、日志汇集与检查命令。同步处理原readback默认值和peer契约版本。

完成条件：Mac适用检查通过；交付可执行的小容量NPU gate及通过/失败判据，按
[阶段交付流程](../verification.md)取得用户硬件确认后才关闭03。

## Acceptance criteria

S2独立gate已通过并获用户确认；以下为整票正式服务验收，仍须结合S3–S6的资源、
PD控制和真实attention执行结果核对，不以独立materialization gate替代。

- [ ] 复用02 backend runtime、P/D writer、统一 tick、准入和 drain，不重复开发真实 P
  offload；P native HBM cache 继续服务 chunked prefill。
- [ ] 将已验证的 P/D sparse fetch 接入 attention 输入；依据 prompt length/实际写入
  范围区分两个来源，保持 HBM sparse cache hit/miss/refill 和重置行为正确；
  readback关闭时仍校验binding、维护可读范围，padding/未写入位置不污染cache。
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
  验证，正式模型对照矩阵由04完成。
- [ ] 按[阶段交付流程](../verification.md)核对实现、交付脚本并记录用户硬件验收。

## Verification

Mac覆盖S1–S5的行为合同及原模式回归；具体命令以实现后的测试入口为准。
用户运行小容量NPU gate，至少包含零decode、真实decode、连续请求和实际slot复用，
覆盖不同P/D slot、prompt边界、首个decode KV、HBM hit及两路miss，并验证Graph16。
有效KV内容由独立copy gate核对；正式服务日志证明attention取数、Index K/必要辅助
传输、旧存储/流量停用及全rank最终free=16。另验证同版本普通模式恢复旧链路。
HTTP 200或出现Graph日志本身不算通过。
固定greedy token对照和正式模型集成矩阵归04；新数据来源导致输出异常时须在03定位，
不能以04负责正式对照为由交付已知错误。

## Comments

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
