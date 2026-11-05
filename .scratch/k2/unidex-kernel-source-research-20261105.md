# UniDexCopy 内核选型源码研究（2026-11-05）

**2026-11-06 调用范围修正：** 当前 `pool_copy=true` 的 fill/admit 已走SDMA，不属于核替换。原生 Host offload/get_forward_kv 也不是当前mempool路径。已重新核对上游模式与分支，并修正本页第5、6、9节；后续范围以[逐调用点复核](unidex-migration-audit-20261106.md)为准。

范围：交接包中新增内核与 ticket25 的双写、跨区 swap-in，以及后续统一替换当前 UniDexCopy 调用的适配基础。本文件是源码研究，不是实施票或硬件验收。未修改功能实现，未执行 NPU，未重复运行上一轮的整模型证据复算。

## 结论

**首选 `binary-v1` 的通用多基址二分路由内核。** 它支持一次提交从多个执行设备可访问的基址读取，再分别写入多个目标基址。以同一个源行创建两条任务，可以在一次 launch 中完成 ticket25 的两个目标写入；以 HBM LRU、remote BM、local BM 为不同源区，可以一次 gather 到 selected KV。两种用途不要求改写现有 AscendC 核，但需要调用方接入多基址表、全局行号和固定生命周期元数据。[通用核][generic-kernel] [接口合同][generic-api]

它的“多目标”是**每任务唯一目标、多任务分散到多个区域**。同源双写会执行两次 CopyIn，不能宣称单读双写。现有 `banked_fanout` 确实单读双写，但双目标固定为 selected 和下一 HBM cache bank；它不是任意 BM 双目标写算子，不能直接承担 ticket25 prefill 双写。[通用任务循环][generic-loop] [fanout 双写][fanout-pop]

**Graph replay 的内核机制成立，且包内有该通用二分核的真实 NPU Graph 记录。** 当前没有证据证明其在本项目 BM 跨机映射、页表变化和完整控制协议上的组合已经验收。结论应为“可按固定缓冲合同接入 Graph；本项目仍须针对真实映射执行验收”，不能直接写成 ticket25 已通过。[Graph 测试代码][binary-graph] [8 核结果][binary-graph-result]

## 1. 候选版本对照

| 候选 | 路由和地址 | 能否覆盖双写及三源读取 | Graph 机制 | 选型 |
|---|---|---|---|---|
| uint64 单基址 | 单 src/dst，加 64 位行偏移 | 可多次调用；一次不能选多区域 | 现项目已有接入，父报告核对 | 保留现有行为作为迁移对照 |
| `multibase-v1` 通用扫描 | 对每条任务扫描源/目标区间，要求唯一命中 | 能；双写需重复源任务 | 九张固定地址表；数据可变 | 语义可用，优先采用后继二分版 |
| `binary-v1` 通用二分 | 每个活跃核先判断表是否有序；符合则二分，否则逐行扫描 | 能；任意可访问区域；双写两次读源 | 每次回放重新检查表和读取元数据 | **推荐** |
| 旧 batched 3→2 | 使用通用核；Python 构造 HBM cache/Host/staged 到 selected/Host 的任务 | 可借鉴建表方式；实验 manager 不等于 mempool 接口 | 捕获任务构造与通用核 | 参考适配样例，不搬整个 runtime |
| singlebank split/combined | 专用 decode 核；核内判断 cache hit、Host miss、当前 KV | 只允许一个线性 Host 区；不支持 ticket25 多 prefix 段页表 | phase 在捕获时固定；输入 Tensor 内容可变 | 不作为统一后端 |
| banked_fanout | 专用 decode；旧 cache/Host/staged → selected + 下一 cache bank；另写当前 KV 到单 Host | 真单读双写，但双目标类型和布局固定 | 设备 bank 标量每步翻转 | 不直接适配 ticket25；仅作以后扩展通用 fanout 的素材 |

通用扫描版和二分版的差别集中在 `OrderedPrefix` 与 `Resolve`；两者仍是相同的九 Tensor＋`block_bytes`＋`block_dim` 契约。二分版没有把路由证明缓存到 Python 或前一次 replay。[扫描版源码][scan-kernel] [二分判定][generic-route]

## 2. 通用多基址怎样表示实际数据面

### 2.1 表示能力

源和目标各有三张表：`bases:int64[C]`、`ranges:int64[C,2]`、`base_valid:bool/uint8[C]`。另有等长的一维 `src_index:int64[T]`、`dst_index:int64[T]`、`valid_mask:bool/uint8[T]`。所有九张表必须是当前 NPU 的 contiguous Tensor。[Host 校验][generic-host]

逻辑行 `r` 唯一落入 `[begin,end)` 后，实际地址是：

```text
base + (r - begin) * block_bytes
```

这不是逻辑 token 自动查页表。页表、层号、物理页号必须由调用方先转成对应区域的物理行偏移。不同源区域分配互不重叠的逻辑行编号区间即可，逻辑区间顺序与物理地址大小无关。[地址解析][generic-address]

对 page-first `[physical_page, layer, token_in_page, ...]`，可把整区映射成连续的 `row_bytes` 行，并以如下表达式生成源索引：

```text
source_index = region_logical_begin
             + physical_page * num_layers * page_size
             + layer * page_size + token_in_page
```

HBM LRU 对应自身的 cache-row 索引；remote alive、local alive、prefix 各段使用各自的 base 和逻辑区间。无需为了每个散页增加一个 base 条目；同一映射区内的散页继续用 64 位行偏移。不同区域也不能复用同一逻辑区间来表达复制，否则重叠会被判为歧义而跳过。[解析语义][generic-route]

### 2.2 ticket25 双写

主 KV 的任务可展开为：

```text
task i:     src = source_row[i], dst = D_alive_logical_begin + D_physical_row[i]
task N + i: src = source_row[i], dst = prefix_segment_begin + prefix_physical_row[i]
```

一个 launch 覆盖两组任务。候选 prefix 分配失败时，只关闭第二组任务 mask；如果需要固定 Graph，则保持任务容量为 `2N_max`。源只需一个 base；目标 base 可以包含 D 的实际目标区域及多个 prefix 段。源数据在本次 copy 中不能被写入；所有有效目标写必须不重叠。各 rank 的 owner 掩码、页分配和字节计数仍由 mempool 负责。[通用任务循环][generic-loop] [非 alias 合同][generic-api]

每任务只有一个 `dstAddress`，执行一次 `CopyIn` 再一次 `CopyOut`；重复源任务是两次源读取。若目标是“一次 HBM 读取再写两个 BM 目标”，现有通用 ABI 需要额外增加第二目标元数据/地址，不能通过当前两张重叠目标区间获得广播。[通用 CopyIn/Out][generic-copy]

一次调用只有一个 `block_bytes`。主 KV 1152 B、Index K 256 B 不应混进同一个调用；分别构造任务。`block_bytes` 上限 32768 B 表示每任务拷贝块宽，不是模型 page 大小；page64 的主 KV 单层页 73728 B 不能作为一条任务，按 token 行拷贝则无此问题。[Host 行宽限制][generic-width]

远端可见性、两目标失败、`ready_tokens`、publish、页引用与释放不由本核处理。本核没有完成计数或 per-task 错误 Tensor；无匹配、歧义、负索引、无效 base、算术溢出都会静默跳过任务。不能以“launch 成功”代替“所有应写数据已完成”。这是现有接口语义，不建议借迁移额外堆叠防御逻辑；应沿用当前协议验证并在适配验收中覆盖预期有效任务。[接口合同][generic-api] [ticket25 合同][ticket25]

### 2.3 swap-in 一次读 HBM＋remote DRAM＋local DRAM

kernel 不检查“HBM/Host/BM”介质标签。只要传入执行 NPU 实际可访问的地址，它便可按每任务的 `src_index` 从对应 base 取一行。因此三个来源及多个 prefix 段可以同时列入源表，目标只有 selected KV base；每个输出行恰好选择一个来源。[通用解析][generic-kernel]

可先在图内用现有 hit/miss、页表和有效位生成一个长度固定的 `src_index`：命中时指向 HBM LRU 区间，未命中时指向 remote/local/prefix 对应物理行。这比把若干互斥任务列表拼成更长列表更紧凑；两种表示均被通用核支持。该建议是调用方适配推论，包内没有本项目的三源 BM 实现。

**“一次调用”不等于所有介质一定充分并行，也不保证端到端加速。** 内核按连续任务块分给各 core；混合延迟和有效任务分布会影响耗时。它的收益需按本项目真实 batch、mask、区域数和计算重叠确认。[任务分工][generic-loop]

## 3. 为什么不用 singlebank / fanout 直接承接

singlebank 的 Host 来源由一个标量 `hostAddress` 和 `req * hostStride + token` 决定。cache/staged/selected 必须为当前设备的连续 Tensor，Host 只有一个入口；Host 写回也只有 `offloadRows[batch]`，语义是一批 decode 当前 token，而不是整个 prefill prompt。`phase=0` 只是把四类 decode 任务合并，不增加任意基址页表能力。[singlebank Host 校验][single-host] [singlebank 路由][single-route]

fanout 与此类似。读取旧 bank 的一行后，把同一 UB Tensor 写到 selected 与下一 bank。下一 bank 地址在核内由 `cacheBase + newOffset + req*cacheStride+k` 算出，无法传两个独立 BM 目标。staged→Host 的那条路径调用 `Push(source,target,0)`，本身只有一个写目标。[fanout 路由][fanout-route] [fanout Host 校验][fanout-host]

因此，fanout 可提供“把一次 CopyIn 的 UB 数据输出两次”的底层写法，但把它用于 ticket25 需要重做路由/API及页表输入。直接选择通用核可复用现成多区寻址和 Graph 元数据，不必引入 cache bank 翻转、更大的 HBM 缓存或专用 decode 激活条件。

## 4. Graph replay 的判定与适配条件

### 4.1 内核本身

Host launcher 只读取元数据 Tensor 的形状/设备/dtype，取 `getCurrentNPUStream()`，为九张表 `record_stream`，并通过 `RunOpApi` 发射 `ACLRT_LAUNCH_KERNEL`。索引值、base 值和 range 值由设备核执行时读取。每次调用都会重做有序区间判断。因此同一个已捕获 kernel 节点能使用更新后的表内容。[Host 发射][generic-launch] [二分判定][generic-route]

保持固定：九张元数据存储地址、形状/容量、任务数 `T`、行宽、block_dim。可图外或图内更新：bases/ranges/index/mask 的内容。更新需先于本次 replay，且本次执行期间保持稳定。真实 BM 映射、HBM 分配及其 owner 必须覆盖最后一次使用；`record_stream` 元数据不会持有裸地址指向的 BM 存储。[接口 Graph 合同][generic-api]

全无效任务应保留正容量表和固定任务数，设置 mask 为 0。若 capture 时 `T==0` 或 base capacity 为 0，Host 直接 return，图内就没有 copy 节点；之后不能靠填表使一个不存在的节点出现。[空容量返回][generic-empty]

### 4.2 描述符封装不能盲搬

实验 `_descriptor()` 在 Python 中创建 pinned Host packed 表并 `.to(device)`。捕获时绕过 LRU cache，且把 Host/Device 描述符保留到服务退出；这样它证明了固定服务里的捕获资源存活。[实验描述符][descriptor]

但若迁移时打算在图外更新 device base/range 表，不能忽略 capture 中的 H2D 描述符初始化：旧 Host 内容可能在 replay 时重新上传。最简方案是建立图外持久描述符，固定区域 base/range 和容量，只有页索引、状态、mask 按既有设备页表流程更新。若 base/range 本身也需变，更应明确一次更新的 stream/event 顺序。不能用新增 Python 指针列表替换已经捕获的 metadata Tensor。

三源 gather 与后续 refill 之间还存在数据依赖。通用 API 禁止本次 source/destination alias；若先读旧 LRU 又在同一调用内覆盖该 LRU，多个 core 无全局阶段屏障，不能任意融合。可以统一底层 kernel，同时保留 gather→attention/refill→slot-map 的既有 stream/event 边界。[非 alias 合同][generic-api] [通用任务循环][generic-loop]

### 4.3 已有证据与缺口

- 通用二分：单核和 8 核的结果各记录 14 eager 用例与一次 capture、8 replay；表在 sorted、nested overlap、unsorted 之间切换，指针/shape 不变。两份 JSON 为 PASS，8 核库 SHA 为 `27c783f1f855fbd66225b574a65f35d75fd1b21d5dc5534f282c25b1d95c973c`。这是 HBM 根缓冲用例。[Graph 测试][binary-graph] [1 核结果][binary-one-result] [8 核结果][binary-graph-result]
- 通用多基址还保存 2→3→2→0→恢复等活跃源/目标数变化的固定容量 Graph 测试；会修改实际 base 值、range 起点、索引和 mask。并不是每次重建图。[动态表 Graph 测试][multi-graph]
- mixed NUMA 用例同时包含 HBM、node0、node7 注册主机页，并核对字节与 guard；分配方式是 `create_shm_tensor`，只使用 device0，属于同机 NUMA 证据，不是跨机 BM。[NUMA 实验][numa-evidence] [分配代码][numa-code]
- native-vs-hybrid 的真实 manager 把 HBM hit 与一个注册 Host miss 合并。旧 batched 再增加 staged 当前 KV，构成三源到 selected/Host 两目标。这些例子证明了通用表的接入方式，但其“三源”不是用户要求的 HBM＋remote BM＋local BM。[hybrid 两源][hybrid] [batched 三源][batched]
- singlebank/fanout 的整模型 Graph 证据不等于它们能接本项目 BM 页表。其已验证几何、单请求和专用线性 Host 布局应保持与“任意多基址核能力”分开。

## 5. 统一替换时的落点建议

采用同一通用多基址核覆盖 1→1、N→1、1→N、N→N；在调用方建立薄适配层，既能接旧单基址包装参数，也能接已有 mempool 的区域表与任务索引。单基址调用可视作源表/目标表各一个条目；多源 swap-in 则真正合并来源，不能只把旧调用逐个套壳便声称一次 gather。

独立插件版本 `unidex_copy_multibase` 与 `binary-v1` 内核逐行比较仅导出符号不同。其 Host 注册了新算子名和 11 参数 schema，可与原 12 参数 `unidex_copy` 共存；这是迁移期间比覆盖整库旧同名 ABI 更直接的入口。实验写死的 `/unidex-hybrid/...so` 加载路径须接入正常部署，但无需带入其 service probe、全局永久描述符列表或专用 manager。[独立插件 Host][plugin-host] [插件来源记录][plugin-identity]

“所有 UniDexCopy 使用点统一”与“所有搬运都融合为一次调用”是两个范围。统一后端可以保持必要的先后顺序、不同 block_bytes 和不同完成语义。BM 库的 host batch/wait、传输 fence、load_back 的集合通信等不能仅因也是 copy 就替换成裸 UniDexCopy；是否替换须由父报告的当前调用点清单逐项判定。

如果先开基础迁移票，建议其边界是：统一 API/插件装载、当前5类活跃操作的核迁移、swap-in合并、固定描述符与Graph回放。仍保留的非mempool/旧核分支可随公共接口做兼容，但不计作当前路径；`pool_copy=true` 的fill/admit继续走SDMA。ticket25再改变页放置、双写、prefix引用、publish；ticket20再做多chunk。本轮只是建议，未建票或改状态。

## 6. 当前调用点：按实际配置核对后的范围

2026-11-06重新从验收配置、模式选择和上游调用核对。当前 `pool_copy=true`，P用mempool写回、D用mempool稀疏读取；不能根据搜索到函数名就认定当前会执行。完整条件、12处数据提交点及验证入口见[迁移范围复核](unidex-migration-audit-20261106.md)。

| 当前活跃操作 | UniDexCopy实际提交点 | 建议替换与融合范围 |
|---|---|---|
| P/D主KV写回、P Index K写回 | [mempool/offload.py:117][pool-offload] | 改1→1；保留writer分工与不同类型行宽 |
| HBM cache hit→selected | [sparsity manager:1022][native-materialize] | 与下两项合并物化 |
| 当前token HBM→selected | [mempool/decode_write.py:67][pool-current] | 合并，但保留当前token直供语义 |
| P远端alive、D本地alive→selected | [mempool/copy.py:141][pool-fetch] | 两路BM与hit/current合为一次gather |
| selected→HBM回填 | [sparsity manager:1079][native-refill] | 改1→1，等待旧cache读完 |
| slot-map更新 | [sparsity manager:1118][native-slotmap] | 改4B行的1→1，保留独立调用 |
| prefix主KV/Index K load_back的gather | [mempool/prefix.py:868][pool-loadback] | 同层同类型可合并多段，后续all_gather/scatter保留 |

这些是5类业务、7处提交代码位置。`indexed_copy.py`是共用几何/地址适配层，不是另一种独立业务操作。

另有条件兼容项：原生manager的748（offload_v2）、859（get_forward_kv）、1054（Host miss）只在相应非mempool路径使用；677（offload v1）未找到生产调用者。它们不能计为当前P写回、当前load_back或当前D的Host miss。保留这些接口时，通过统一封装兼容。

`prefix.py:1012` 的 `_copy_region` 仅供未启用执行器的旧fill/admit分支。当前配置从312/545进入 `_submit_pool_copy`，由 [pool_copy.py][pool-sdk] 执行 SDK `G2G batch/wait`。**当前fill/admit不替换为multibase。** load_back和旧分支共享的核封装可以统一，但不能改变执行器分支选择。

独立脚本的旧ABI、Graph schema和CPU注入替身要跟随公共边界适配。`verify_pool_sdma.py`替换的只是读回核对；`bandwidth_device.py`替换的是读操作/读回，其SDK L2G写入保留。`verify_resources.py`覆盖原生Host兼容接口。测试导入桥复用生产代码，不维护第二份实现。

load_back的all_gather、native/NZ scatter、slot_map_lookup以及SDK batch/wait保留现有职责。

### 当前 swap-in 实际有四个逻辑来源

现有 mempool 模式每层的物化为：HBM cache hit 一次；当前 token HBM 暂存一次；P alive 远端一次；D alive 本地一次。后面另有 refill 和 slot-map 各一次。这里统计 capture 中保留的调用节点；某一步的实际有效行可以为零。[物化流][native-materialize] [当前 token][pool-current] [两路 BM][pool-fetch]

合并方案：每个 selected 输出行只创建一条任务，优先保持现有 hit 判定；miss 且为当前 token 时读 HBM 暂存；其余根据设备页表选 remote/local。这样四次物化可变为一次，另两次 refill/map 保留。HBM 在介质分类上是一类，在地址表中通常有 cache 与 current 两个条目。

25 后主 KV 的源表变为 HBM cache、HBM current、D alive、P prefix 各段；未来本地副本也是同一表的新条目。统一适配层负责地址与行索引；策略层仍决定命中、页归属和副本选择。

不能将 gather 和覆盖同一 HBM cache 的 refill 塞入同一通用调用。不同 core 没有跨任务的全局“先读完所有旧行、再写所有新行”屏障，可能覆盖尚未读取的旧 cache。通用多基址迁移不要求采用双 bank。

## 7. Graph 接入的具体判断

**判断：支持同一 Graph 的正常 replay，条件在当前架构中可以满足；本项目组合仍待 NPU 证明。** 包内通用二分核的 1 核/8 核原始 JSON 均为 PASS，各记录 14 eager 用例与一次 capture 后 8 次 replay。本轮直接读取这些 JSON，并核对独立插件 kernel 与 `binary-v1` kernel 在仅替换导出符号后文本完全一致。未重跑硬件，也未宣称完整端到端证据已重新复算。

已有 `enable_page_tables` 在捕获前分配表，`install_pages` 原地写内容，更新在 binding event 前完成；这正好能承接多区域索引。需要增加区域选择信息，保留 lease/version 检查与页表的固定地址，而不是每个请求重建图。[稳定页表][pool-pagetable]

对当前 view 还要避免重复加层偏移：`device_base` 已经含 `layer × B × row_bytes`，`map_rows` 仅加 `physical_page × L × B + token_in_page`。若新表使用整区 base，层偏移应进入 index；若沿用逐层 view base，就不能再在 index 中加一次 layer。[view 几何][pool-view]

接入时的必要约束：

1. 描述符在图外建立，按 device/layer/capture 持有。shape 相同但 output 地址不同的两次 capture 不能误用同一目标描述符。
2. 图外输入页表与绑定、图内生成的索引/mask 均保持 storage 有效；只原地更新内容。正容量全 invalid 的 warmup 也必须捕获 copy 节点。
3. 合并流等待 `copy_ready` 后同时读取各来源；attention/refill 等物化完成。slot-map 的既有顺序继续遵守。原来两个事件可指向同一物化完成边界，但应明确改接，不能遗漏等待。
4. Graph 外继续执行 `begin/end_forward`、完成事件查询和控制协议。Python 层 hook 的统计不能期待 replay 每次再执行。[现有完成机制][pool-completion]
5. BM 映射与数据 owner 保留到最后一次使用；需要换指针时更新的是固定描述符里的 int64 内容，不能仅重绑 Python 变量。

TorchNPU 官方文档也要求向捕获地址 `copy_()` 新数据，重新赋值不会改变已捕获的地址。这只是框架通用规则；这里的自定义算子可捕获结论主要来自交接包对应版本的 launcher 和设备实验。[官方 NPUGraph 说明](https://github.com/Ascend/pytorch/blob/master/docs/zh/developer_notes/npugraph.md)

### 远端写还缺一层证据

14 已接受的双写是 **BM SDK L2G + wait**；新核远端写则是 **AscendC DataCopyPad → BM 设备映射**。两个路径的传输实现和完成边界不同，不能把 14 的 93.713 GB/s 和完成性直接算给新核。[14 原始测量路径][bandwidth-sdk] [已接受结果][ticket14-results]

新核能计算并发射两个目标写，未发现需要先修改 kernel 才能表达双写的结构限制。但能否对本项目远端映射正确写入、发送完成事件后 D 侧能否马上读到全部内容，需由真实双机映射微测确认。现有 device mask 计数也不能证明 kernel 没有因错误 range 而跳过任务；应以正确建表加定向内容验收建立依据。

因此不以“核支持 replay”推导“远端写可见性已经证明”。若远端写 gate 未通过，三源读取的迁移结论仍独立成立；25 的写通道应继续依据已验证 SDK 路径作选择，不能放宽 ready 条件。

## 8. ticket25 文案中应先修正的范围

设计原文已经给出明确分流：新算 prompt 页与生成页读 D 本地，命中 prompt 页引用 P prefix 正本并远端就地读。ticket25 的“decode 按源只有 D 本地”不能覆盖有 prefix 命中的请求，否则与“op1 零拷贝”冲突。建议将判据写成“新算 prompt 与生成页无 P alive 远端读；命中页允许 P prefix 远端读”，并分别计数。这是按现有设计消除歧义，不是新增放置策略。[设计流程表][placement-source] [25 验收文本][ticket25]

Index K 同样不应机械理解为增加 D DRAM Index K 池。现有 D 区域只有主 KV alive；设计要求 Index K 写 P prefix 候选区并保留原生 P→D 传输。主 KV 1152 B、Index K 256 B、slot-map 4 B 分别调用，保证各自写者与计数。25 的字节验收应与这些实际目标对应。[区域定义][pool-regions] [25 不变项][ticket25]

## 9. 建议独立迁移票，再推进25与20

建议新增一张基础票（可编号26）：**UniDexCopy 多基址统一适配与 swap-in 合并**，执行顺序为 `18 → 26 → 25 → 20`。编号26仅为建议，本轮未创建 ticket、未修改依赖或状态。

拟定范围：

- 正式接入通用二分核。优先沿用现有独立算子名 `unidex_copy_multibase`，避免整库同名 ABI 从12参变11参影响旧调用。普通路径统一到薄适配层，迁移完成后再清理无用包装。
- 迁移第6节的当前活跃核调用；保留的条件分支单独做接口兼容。fill/admit的SDMA路径不改。将HBM hit/current、remote/local四路物化合为一次，保留page-first、writer、热缓存策略和来源统计。
- 提供最小双机无模型入口：真实 BM 的三类介质混读、HBM→远端 alive＋本地多段 prefix 的双写、固定 Graph 的连续 replay与页表/有效位变化。两个方向放在同一进程/会话里，复用初始化。
- 在该 gate 后进行一次精简模型服务回归：复用已接受的64/512配置、既有答案边界，比较原参考与迁移后完整输出；只采必要的少量 Graph 证据和物化阶段时间，默认关全程 trace。
- 一个 ticket 独立交付与验收，CPU 不代替 NPU。25 再处理双写开关、候选页预留、prefix 引用、零copy publish与真实完成；20 再做多 chunk。

不建议当前引入通用“单读双写”新 ABI。重复源任务已经能满足25的逻辑双写；若真实写入测量显示源读取成为瓶颈，再从 fanout 的 UB 单读双写实现提取独立优化。也不把合并调用数下降当作性能验收通过：交接的通用 hybrid 没有获得端到端加速，singlebank 的65%局部收益来自另一个专用核。[交接分轮结论][handoff-results]

本轮状态：源码/合同分析已完成，保存调用清单与建议顺序；只写研究文档和可视化摘要。未修改生产实现，未启动设备或服务，未关闭或解锁任何 ticket。

## 源码与证据索引

[native-manager]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:677
[native-prefill]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:859
[native-materialize]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1022
[native-refill]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1079
[native-slotmap]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1118
[pool-offload]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/offload.py:117
[pool-fetch]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/copy.py:141
[pool-current]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/decode_write.py:67
[pool-loadback]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:868
[pool-regioncopy]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:1012
[pool-adapter]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/indexed_copy.py:23
[pool-sdk]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/pool_copy.py:64
[pool-pagetable]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/manager.py:589
[pool-view]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/manager.py:760
[pool-regions]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/layout.py:162
[pool-completion]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:927
[bandwidth-sdk]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/ascend-mempool-test/src/ascend_mempool/bandwidth_device.py:95
[ticket14-results]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/.scratch/k2/verification/ticket14-001/results.md:1
[placement-source]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/.scratch/paperwriting-sparse/14-hit-page-placement.md:77
[handoff-results]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/KV搬运优化交接.md:125

[ticket25]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/.scratch/k2/k2-tickets.md:265
[generic-kernel]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_kernel/unidex_copy_kernel.cpp:11
[generic-loop]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_kernel/unidex_copy_kernel.cpp:38
[generic-route]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_kernel/unidex_copy_kernel.cpp:81
[generic-address]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_kernel/unidex_copy_kernel.cpp:106
[generic-copy]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_kernel/unidex_copy_kernel.cpp:163
[generic-api]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/python/sgl_kernel_npu/sgl_kernel_npu/sparsity_driven_kv_offload/ops.py:74
[generic-host]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_host/unidex_copy.cpp:25
[generic-width]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_host/unidex_copy.cpp:68
[generic-launch]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_host/unidex_copy.cpp:85
[generic-empty]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_host/unidex_copy.cpp:79
[scan-kernel]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/multibase-v1/csrc/sparsity_driven_kv_offload/unidex_copy/op_kernel/unidex_copy_kernel.cpp:74
[single-host]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/singlebank-multibase-20261105/plugin/host.cpp:11
[single-route]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/singlebank-multibase-20261105/plugin/kernel.cpp:70
[fanout-route]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/decode-fanout-20261105/plugin/kernel.cpp:69
[fanout-pop]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/decode-fanout-20261105/plugin/kernel.cpp:132
[fanout-host]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/decode-fanout-20261105/plugin/host.cpp:11
[descriptor]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/singlebank-multibase-20261105/graph-runtime/split.ops.py:137
[binary-graph]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/verify_binary_routing.py:112
[binary-one-result]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/evidence/binary-routing-910c/semantic1.json:1
[binary-graph-result]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/evidence/binary-routing-910c/semantic8.json:1
[multi-graph]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/verify_multibase.py:176
[numa-evidence]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/evidence/numa-paths-910c/README.md:25
[numa-code]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/verify_numa_paths.py:94
[hybrid]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/native-vs-hybrid-20261105/graph-runtime/hybrid.ops.py:198
[batched]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/decode-batched-multibase-20261105/runtime.patch:339
[plugin-host]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/native-vs-hybrid-20261105/plugin/host.cpp:106
[plugin-identity]: /Users/hibikid/Documents/sparsekv-ascend-sglang/unidex-kv-handoff-20261105/binary-v1/tests/manual/unidex_copy/native-vs-hybrid-20261105/plugin/source-identity.json:1
