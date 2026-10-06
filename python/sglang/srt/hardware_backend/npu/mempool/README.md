# Ascend mempool storage

提供Ascend PD的存储布局、BM handle/view、KV writer/fetch和backend runtime。
开启`SGLANG_NPU_ENABLE_MEMPOOL=1`选择正式P/D模式，BM/runtime在Graph前创建，
service/control在既有AscendKVManager建立后附加。关闭该开关恢复普通sparse PD。
正式D从HBM cache及P/D BM取数，不分配旧host SHM或main-KV staging；P保留native KV
供prefill使用，PD只注册/发送Index K及必要state/aux。

01、02及03 S5当前小容量验收已获用户确认。S6.2已删除shadow模式与旧host参考
READBACK，实现和验证入口对应正式/普通模式。S6.3整理与最终NPU复验待执行，03保持open。
历史证据见[02总结](../../../../../../.scratch/ascend-mempool/ticket-02-summary.md)及
[03票面](../../../../../../.scratch/ascend-mempool/issues/03-prefill-direct-offload.md)；
当前命令见[正式服务验收](../../../../../../ascend-mempool-test/FORMAL_SERVICE.md)。

## 文件与接口

| 文件 | 接口 | 职责 |
| --- | --- | --- |
| `config.py` | `MempoolConfig.make_mla_layout()` | 以实际 local layer 数、`kv_lora_rank`、`qk_rope_head_dim` 构造 P/D 布局；固定 16 个 slots，容量默认各 16384。 |
| `layout.py` | `KVLayout` / `PoolLayout` | 计算逻辑 shape、row/element offset、贡献大小、共同 stride；校验 BF16 和 UniDexCopy 范围。 |
| `manager.py` | `MempoolKVManager.initialize_rank_pair()` | 将 TP rank `i` 映射到 P 的 `base_port+i` store，设置 P/D 的 BM rank 0/1，启动 BM 并 join pool。 |
| `manager.py` | `MempoolKVManager.create/join/view/close()` | 拥有一个双 rank BM handle，验证映射，提供 view，并在 drain 后销毁。 |
| `manager.py` | `MempoolKVView` | 提供一个 layer 的逻辑 tensor、元素地址和同步 setup 写入；持有 manager 引用。 |
| `diagnostics.py` | `startup_stage()` | 记录启动步骤和耗时；诊断开关打开后采样执行线程、主机/容器内存，后台线程不调用 BM/NPU。 |
| `offload.py` | `MempoolKVOffload.write(values, *, slots, positions, valid)` | 通过显式 metadata 将 temporary KV 写入本侧 BM；不持有另一套输入缓存。 |
| `copy.py` | `SparseCopyInputs` / `SparseKVCopy` | 从独立 gate 提升的共享 P/D UniDexCopy 路由；支持复用固定copy metadata。 |
| `copy.py` | `KVFetch` | 正式D的BM miss直接写入调用方selected KV；同形状目标更换时保留固定输入metadata，两次copy分别读取P/D。 |
| `rows.py` | `derive_kv_rows()` | 从普通 forward 张量推导 request row、全序列 token position 和 valid；目前有意保留 sparse manager 行推导的副本。 |
| `runtime.py` | `MempoolRuntime` | 持有固定设备 binding 表、per-layer writer、forward/Graph 边界及本地写入计数/完成事件。 |
| `runtime.py` | `selected_kv_valid()` / `fetch_selected_kv()` | 正式模式统一hit/miss可读范围并填充BM misses，完成后检查每层覆盖/非法读取。 |
| `runtime.py` | `KVRowBinding` / `KVWriteReceipt` | 标识一次本地 row attachment，保留 detach 后的完成事实；不表示 PD slot ownership。 |
| `runtime.py` | `initialize_for_model_runner()` / `model_forward_scope()` | Graph前建立BM/runtime；逐次eager、warmup/capture和replay的host边界。 |

PD接入仅新增 `disaggregation/ascend/mempool_service.py` 和 `mempool_tick.py`：
service投影真实Req并延迟native清理，tick统一TP observations/preflight/commit/outbox。
协议仍由原control拥有；服务读取实际BM nonce确认ZMQ peer与BM peer一致。
`config.py` 集中校验启动组合；NIC基址为每对留出2个端口，store仍为 `base_port+i`。

每侧 KV payload 为 `L * B_slots * S * N * D * 2` 字节；MLA 的 `N=1`，
`D=kv_lora_rank+qk_rope_head_dim`。每 rank 加 64 字节 probe，再向 1 GiB 对齐。
`create2` 的 local contribution 分别使用 P/D 的大小，共同 maximum 使用较大值。
每 layer span 必须不超过 `UINT32_MAX`，每 row 不超过 32 KiB；整个 pool 可以超过 4 GiB。

## 从模型加载到 mempool ready

`ModelRunner.alloc_memory_pool()`先调用sparse配置模块的`configure_for_model_runner()`，
解析并保存`SparseKVOffloadMode`，验证`MempoolConfig`及可提前确定的layout约束。
此时不导入MemFabric、不建立BM连接。容量估算、native pool、backend和sparse manager
沿用同一mode，分别使用`uses_sparse_kv_cache`、`uses_host_kv_offload`、
`uses_pd_decode_staging`、`uses_mempool_bm`表达职责。
S3中正式D不分配host SHM、指针映射、host length tensor和staging copy stream；
host metadata容器保持为空。普通模式仍分配并使用原资源。
正式PD buffer只发布Index K；P native K/V和D Index K仍分配。

随后`ModelRunner.init_attention_backends()`构造attention backend，再调用
`runtime.initialize_for_model_runner()`消费已校验配置。因此进入BM前，模型和原生NPU KV已存在；
D的`SparseKVCacheManager`仅分配HBM sparse cache/slot map，P仍使用原生NPU KV。
正式D不分配旧hostSHM/staging；独立BM gate没有模型和服务分配。

```mermaid
flowchart TD
    A[模型已加载，尚未分配KV] --> B[解析mode并校验MempoolConfig及layout]
    B --> C[按同一mode估算容量并分配native KV / Index K]
    C --> C1[构建attention backend\n正式D仅创建HBM sparse cache与slot map]
    C1 --> D[mf.initialize]
    D --> E[initialize_rank_pair\nD等P store可达，然后bm.initialize]
    E --> F[bm.create2\n分配本地DRAM并建立BM handle]
    F --> G[handle.join并等待两侧设备映射]
    G --> H[分配MempoolRuntime固定表与writer\nattach到attention backend]
    H --> I[后续Graph初始化与PD控制握手]
```

`initialize_rank_pair()`中，TP rank `i`只和另一台机器的同号rank组成一个BM world，
world size始终为2。P是BM rank0，D是rank1，store为`P:base_port+i`，
NIC基址为`nic_port+2*i`；进程内锁防止本worker重复初始化BM，并非16个rank之间的锁。
D的TCP探测只证明listener可连接，正式peer注册仍由BM完成。

`create()`调用双方gate与服务共用的`bm.create2()`：本地仅贡献DRAM，HBM大小为0，
使用SDMA，P/D共同的`max_dram_size`是较大的单rank贡献。贡献大小为
`align_up(layers*16*capacity*1*(kv_lora_rank+qk_rope_head_dim)*2 + 64, 1GiB)`。
例如每rank贡献11GiB时，双rank预留GVA跨度为22GiB，每个进程实际贡献11GiB本地DRAM；
16个TP worker每侧合计176GiB，另加模型、native KV或sparse cache等资源。

`join()`返回并不保证对端已经可供NPU访问。因此代码随后检查本地实际贡献、P/D GVA
间距，以及每层首尾和贡献末尾的device VA连续性；全部成功才发布`view()`可用的地址。
P端缺少rank1映射时应结合D日志判断：D未返回`create2()`时，该映射暂缺并不证明GVA
计算错误。映射完成后，runtime分配固定binding/counter表和逐层writer，再attach到backend。
`[MEMPOOL_INIT] READY`仅表示这部分完成，服务准入还需要后续PD control ready。

### 按 TP rank 指定本地 NUMA 节点

在P、D各自启动服务的shell中，列出希望使用的本机NUMA节点：

```bash
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
```

worker按列表顺序轮转，`numa_node = nodes[tp_rank % len(nodes)]`，
传给`bm.create2()`的`flags = 0x80 | numa_node`。这里使用真实TP rank，
与P/D的BM rank、NPU device ID无关。选择`0,2,4,6`时：

| TP ranks | 请求 NUMA 节点 | BM flags |
| --- | --- | --- |
| 0、4、8、12 | 0 | 128 |
| 1、5、9、13 | 2 | 130 |
| 2、6、10、14 | 4 | 132 |
| 3、7、11、15 | 6 | 134 |

节点ID可不连续，顺序保留，允许逗号两侧有空白；例如`6,0`让TP0/2/4/...选6、
TP1/3/5/...选0。TP数量不能整除节点数时，各节点池数最多相差1。P/D可配置不同列表。
`0,2,4,6`用于规避当前机器奇数节点的`HalMemCreate ret:6`，不代表SDK问题已修复。
节点存在性检查也不能证明HAL支持在该节点分配。实际物理落点仍需通过MF日志和
各节点内存增量核对。双机16池测试见
[偶数 NUMA 测试说明](../../../../../../ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md)。

创建池前会用`/sys/devices/system/node/online`检查整份列表。若包含本机不存在或
未在线的节点，打印WARNING，指出无效ID及本机在线节点，整份配置回退为`flags=0`。
空列表、重复ID、非法格式、ID不在0..126内或无法读取/解析本机拓扑，也提示后回退；
ID127被MF保留为自动亲和模式。回退发生在调用BM分配之前，不在HAL分配失败后重试。
例如本机在线节点为`0-7`时，`0,8`整体回退，TP0也不会显式绑定到0。

未设置变量时直接使用`flags=0`，不读取拓扑。恢复默认模式可执行
`unset SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE`。旧的
`SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE_COUNT`已移除，不再影响分配。
配置在创建新pool时生效，修改环境变量后须重新启动服务，已存在的pool不会迁移。
该变量控制mempool本地DRAM的NUMA请求，不设置CPU绑核或原hostSHM的策略。

`Creating mempool BM pool`和`BEGIN stage=bm.create2`会记录`tp_rank`、
`local_numa_nodes`、`numa_node`和`bm_flags`；未启用或回退时节点为`-1`、flags为`0`。
`local_dram_bytes`仍为该rank的完整贡献，按同一NUMA上的rank数累计预算。
直接调用`MempoolKVManager.create()`时，有效的显式绑定须同时提供非负整数`tp_rank`。

## 启动卡住时的诊断输出

默认在调用边界记录`[MEMPOOL_INIT] BEGIN/END/FAIL`，含stage、PID/TID和耗时。
两侧在原服务启动命令前设置`SGLANG_NPU_MEMPOOL_DIAGNOSTICS=1`后，还会：

- 把MF进程级日志设为INFO，显示HAL分配耗时、export/import/map等SDK日志；此设置也影响
  同一进程后续TransferEngine日志。
- 为未返回的调用每15秒输出`WAIT`及执行线程的`wchan`；第一次WAIT附Python栈、
  内核栈和资源快照。`/proc/self/task/TID`指向执行BM调用的线程，避免误采样监控线程。
- 在`bm.create2`前后输出进程RSS、CPU/Mems允许列表、主机meminfo、NUMA节点meminfo，
  以及按`cgroup`/`mountinfo`解析出的v1/v2内存用量、限制与可见父级限制。
- 输出布局、已有hostSHM层数和字节数、实际current device、MF模块路径与相关环境开关。
  `existing_host_shm_bytes`是原sparse manager持有的KV tensor字节数，不代表进程所有DRAM。

| 最后未结束的stage或日志 | 当前等待位置 |
| --- | --- |
| `bm.wait_store` | D还未连上对应P的store |
| `mf.initialize` / `bm.initialize` | MF全局初始化或BM会话初始化 |
| `bm.create2` | SDK建池；结合MF日志和wchan区分HAL分配等内部阶段 |
| `bm.join` | SDK join调用尚未返回 |
| `bm.inspect_pool` | 查询本地贡献大小或两侧GVA |
| `bm.wait_mappings` / `MAPPING_PENDING` | 缺少指定rank/GVA/offset的设备映射 |
| `runtime.allocate` / `runtime.attach` | BM映射已就绪，正在构造或挂接runtime |
| `bm.close_drain` / `bm.leave` / `bm.destroy` / `bm.uninitialize*` | 退出或启动失败后的清理阶段 |

例如`WAIT stage=bm.create2`和`wchan=devmm_master_alloc_interleaving_*`同时出现，
可定位为该线程仍在驱动分配等待；具体分配资源或锁原因还需内核栈/驱动证据。
MF的`Try HalMemCreate ret:... spend time:...`在调用返回后打印，缺少这一行不能单独
证明尚未进入HAL。`WAIT`也不是超时判决，不会取消SDK调用或强制释放pool。

Docker可能禁止读内核栈，日志会明确写`unavailable(PermissionError,...)`，不会因采样
失败中断原流程。容器外不可见的cgroup父级约束仍需宿主机核查；host MemAvailable不能
替代容器额度。后台Python线程的WAIT依赖原生调用释放GIL（本地MF release/1.1绑定如此，
远端二进制commit仍以运行日志为准）。重跑命令及回传内容见独立测试README。

## 生命周期与写入约定

1. 调用方先完成进程级 `mf.initialize()`，并负责在现有 TransferEngine 停止后统一
   `mf.uninitialize()`；manager 不关闭这个共享 MF 环境。
2. 每个 TP worker 进程只负责一对 P_i/D_i。`initialize_rank_pair()` 检查 `i` 在
   `[0, 16)`，P 用 BM rank 0 启动 `tcp://P_host:base_port+i` 的 store，D 用 BM rank 1
   连接同一 URL。它拒绝由本 manager 在进程内重复启动第二对 BM pool，初始化 BM、
   创建 handle、join 并检查两侧映射；失败时清理本次创建的 BM 状态。成功返回的
   manager 在 `close(drain)` 后释放它负责的 BM context。低级 `create()/join()`
   仍由调用方负责 BM 初始化/退出。
   MF 1.1 不提供可靠的 Python API 检查外部代码是否已初始化 BM，调用方须保证此前
   没有其他 BM context。这一步只确定 BM store 与两侧局部 rank；后续控制协议还须
   核对 P_i/D_i 的实际身份。
3. `view(rank, layer)` 可引用两侧 KV；运行时写入仅允许本侧 view。
   `write_rows()` 使用同步 BM SDK copy，只用于捕图外的 setup。
4. `MempoolRuntime` 管理固定设备 `row_slot` / `row_prompt_len` 表，初始为 -1。
   `binding = bind(row, slot=..., prompt_tokens=...)` 原地更新表；安装 event 由下次
   forward 等待。接入层按 approved request attempt 保存这个不可变 attachment 对象，
   对真实 `req.kv.req_pool_idx` 调用 `assert_bound(row, binding)`，拒绝旧 attachment。
   D还必须传入已批准的`prompt_slot`并校验范围；D实际写入长度独立维护。
5. `MempoolKVOffload.write(values, *, slots, positions, valid)` 接受连续 BF16
   `[rows, N, D]` temporary KV，以及同设备的 int64 slots/positions、bool valid 向量。
   runtime 在设备上构造这些参数；P eager 每个 chunk 可使用不同的 rows。
   每个 valid source row 对应一个不同的目的坐标；无效/越界行不写入。
   即使全部 invalid，也会提交 kernel，使 zero-valid capture 保留运行时写入路径。
6. 写入不调用 host tensor read 或 synchronize。跨 stream 的 producer/consumer
   依赖由调用方维护。请求容量检查与写入完成状态由后续控制/集成层负责；bounds mask
   不能用来把超容量请求当作成功。
7. 本 manager 管理 pool 的生命周期。request slot 的 acquire/release 与持久 ownership
   留在 PD 控制层；`view.write_rows()` 和 `offload.write()` 均不分配 slot。
   `detach_row(binding)` 在本地 completion 消费后清除 row 映射并返回不可变 receipt，
   不释放 slot。调用方还须保证无未来 row 提交以及 native transfer 安全；正常 P
   须等 handoff 成功，KV_READY 本身不够。P slot 保留到 D 的 DONE，D 则先 whole-D
   drain 再 detach。receipt 按 attempt 保存，不能再以旧 row 查询已复用请求的进度。
8. pool 和 view 必须覆盖整个 Graph 生命周期。`close(drain)` 的 drain callback
   需排空所有相关 reads/writes，并确保 Graph 不会再次提交；失败时不销毁 pool。
   关闭后的 Python view 拒绝返回地址，但已捕获的 raw pointer 无法靠 Python 检查拦截。

## 正式BM fetch（Ticket03 S2）

`PD_DECODE_MEMPOOL`的materialization从runtime取得公共valid mask，先查HBM cache。
hit仍读HBM，miss通过`KVFetch`以prompt length分流到P/D BM，直接写attention持有的
selected tensor。两路copy在同一miss stream依次执行，统一记录`miss_done`，保留
hit/refill/slot-map的既有事件依赖。普通模式仍走原host SHM miss分支。

公共mask排除row 0、未绑定row、负索引和未写范围；已绑定真实row的非法非负索引
在completion后报错并阻止释放。正式writer核对host预期位置及本层连续写入前缀，
fetch上界受本层实际write counts约束，防止陈旧Graph输入把漏写位置误判为可读。
同形状copy更换目标只更新destination，保留Graph仍可能引用的固定输入metadata。

S5已开放正式服务。`verify_fetch.py`通过独立fixture验证真实materialization、
BM与Graph。2026-10-04用户确认该gate在Graph width16、3个真实rows、top-k宽度2048、
block_dim24/48下通过eager及两轮replay，覆盖P/D miss、mixed、all-hit和zero-valid。
本结果不能替代正式服务分配/PD控制及NPU attention验收。详见上方S2总结。

## 正式完成证据（Ticket03 S5）

`fetch_report()`只读取已完成事件对应的计数。每个forward的快照包括逐层覆盖、非法
selection、有效selection数量和P/D miss数；其余有效selection计为HBM hit。
`mempool fetch_result`在D drain完成、row detach之前输出，含实际submitted/written KV、
forward/replay次数、层覆盖及来源计数。zero-decode保持全零，不伪造数值验证通过。
这里没有旧host参考，独立pattern gate和用户curl分别检查数据copy与小题目输出。

`mempool resources`统计真实buffer字节及注册条目；`mempool native_copy`记录成功的
Index K/aux逻辑发送字节数，main KV为零。BM映射与最终free=16仍使用原生命周期日志。
`verify_fetch.py`现在连续提交五步再统一同步；原S2单步硬件通过不能替代这次异步重测。
新服务启动、curl、checker、性能对照和回传项见[正式服务验收](../../../../../../ascend-mempool-test/FORMAL_SERVICE.md)。

## 测试边界

CPU行为测试位于`ascend-mempool-test/tests/unit/`。`test_fetch_layers.py`用独立pattern
检查两层P/D边界、Graph padding和复用，`test_fetch.py`检查范围、事件和连续forward，
`test_pd_service.py`检查正式fetch故障不能释放所有权。
独立测试通过package path加载生产模块，无需安装SGLang或启动server；只替换硬件边界，
不能证明真实NPU、远端copy和Graph已经通过。硬件命令见
[测试说明](../../../../../../ascend-mempool-test/README.md)。
