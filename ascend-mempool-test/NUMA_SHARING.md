# NUMA 共享 ticket01：双 rank 组件对照

入口：`scripts/run_numa_sharing.sh`。P/D 各运行一次，入口自动启动本机两个
worker、协调四个 participant、检查内容、交替运行 independent/shared、收集
报告并清理自己启动的进程。无需启动 SGLang 服务或加载模型。

这是共享01的组件实现。NPU 尚待用户执行；成功标记也不关闭 ticket。
02、03和原04仍按票面依赖等待用户验收。

代码入口：[双机运行器](scripts/run_numa_sharing.py)、[单NPU worker](scripts/sharing_worker.py)。
[SharingPool](src/ascend_mempool/sharing_pool.py)负责物理贡献和映射，
[CopyWorkload](src/ascend_mempool/sharing_copy.py)负责分片、真实copy和内容校验，
[Pipeline](src/ascend_mempool/sharing_pipeline.py)负责stream依赖和selected-ready，
[Cohort](src/ascend_mempool/sharing_control.py)负责四方阶段同步及最终drain。

## 配置与命令

使用同一 superpod 的两台主机／现有容器，两侧同步同一份代码，保留已有
torch、torch-npu、sgl-kernel-npu、MemFabric 及 CANN 环境。完整模式要求
`torch.npu.ExternalEvent`、NPUGraph、多 stream capture、HCCL 和 NPU profiler。
不安装或更新 SDK；入口复用现有测试的 Ascend 环境脚本。

按实际拓扑为每侧指定同一 NUMA 下的两张空闲 NPU，不按编号相邻推断。
NUMA 必须 online，容器 CPU set 必须与该节点相交；worker 将 CPU affinity
限制在交集内。SDK NUMA flags 由显式节点设置，不读取旧生产 NUMA 轮转变量。
默认预留端口19400–19412；可在两侧同时改 `--control-port`。分配已计入
MemFabric 给 NIC base 加 BM rank 的偏移，HCCL rendezvous 使用19412。

先启动 P，再启动 D（P 会等候；D 先启动也会重试）。替换所有尖括号占位符：

```bash
# P 容器，仓库根目录
bash ascend-mempool-test/scripts/run_numa_sharing.sh \
  --role prefill --prefill-host '<P_IP>' --decode-host '<D_IP>' \
  --run-id numa-share-r1 --devices <P_NPU_A> <P_NPU_B> --numa-node <P_NUMA>
```

```bash
# D 容器，仓库根目录
bash ascend-mempool-test/scripts/run_numa_sharing.sh \
  --role decode --prefill-host '<P_IP>' --decode-host '<D_IP>' \
  --run-id numa-share-r1 --devices <D_NPU_A> <D_NPU_B> --numa-node <D_NUMA>
```

两侧 `run-id`、矩阵和计时参数必须一致，NPU IDs 与 NUMA 按本机分别配置。
每次重测使用新的 run-id，旧目录不覆盖。可先在同一命令末尾加
`--smoke --rounds 1 --samples 1 --repeats 16`，另用 run-id；这是双机、双 rank
的 eager smoke，只跑尾部写入和 mixed 读取，不是完整组件验收。

Mac 使用已安装CPU torch的Python环境，可在同一完整命令上添加 `--describe`，
不加载 NPU 库、不联网、不分配 BM。
默认完整矩阵为3轮 A/B、B/A、A/B，每种模式中独立检查 eager/Graph、24/48 cores，
每个 case 暖机3次、3个样本、每样本64次操作。`--timeout` 默认每阶段3600秒。
正式计时与 profiler 单独运行。

### run-2 写后读阻塞的诊断入口

两侧原命令改用新的相同run-id，并追加
`--diagnose-write-read --block-dims 48 --timeout 300`。
诊断仅执行一次independent，保留`write_then_read-1`→`write_then_read-16`的
原有顺序、双consumer及eager/Graph切换；默认仍为3 samples、64 repeats。
不运行shared、多轮对照及delayed-publication profiler。
诊断成功仅输出`DIAGNOSTIC_PASSED`、退出0，scope为`write_read_diagnostic`，
comparison为空；NUMA证据仍如实记录，不能作为完整组件验收。
若成功，只能说明缩小后的场景未复现，不能宣布原完整矩阵的阻塞已修复。

诊断worker将带UTC时间的步骤写入日志及`member-*/progress.json`，每次步骤
推进会重置60秒栈转储计时器；无进展60秒后自动打印Python线程栈并每60秒重复。
这些诊断步骤位于设备计时区间外，不新增设备同步或改变copy/HCCL的执行顺序。
`--timeout`仍是父进程对整个worker的时间预算，不是按case重置的无进展计时器；
300秒超时后还需自身进程的清理时间。诊断失败返回非零，请回传两侧worker日志
最后的`[DEBUG-numa-sharing]`步骤、自动栈及父进程错误；无需等待完整矩阵。

用户run-3-diag两侧通过，缩小后的序列未复现run-2阻塞。下一步使用
`--diagnose-independent --block-dims 24 48 --timeout 2400`及新的run-id，
保留原完整矩阵的所有前置用例、顺序、双consumer、eager/Graph与默认采样参数，
仅执行一次independent并开启上述诊断；不执行shared和后续轮次，也不执行
delayed-publication profiler。scope为`independent_diagnostic`，结果仍仅为
`DIAGNOSTIC_PASSED`或失败，不构成完整验收。两个diagnose选项互斥，也不能与smoke合用。
run-2此前保存的样本覆盖约29分钟；2400秒是本次worker的40分钟预算，另需清理时间，
不是对实际完成时长的保证。若再次阻塞，收集60秒无进展后自动输出的P侧栈。

默认 BF16 compact row 为576维／1152 bytes；2层、16 slots，P 每slot8192 tokens、
D 每slot16384 tokens，top-k2048。每个 owner 额外256-byte identity probe，
按既有1 GiB粒度对齐，故独立模式每侧2 GiB，共享模式每侧1 GiB。
这些是本次实验容量，不是容量上限或TP16服务容量结论。报告分别保留payload、
probe、实际SDK物理贡献与VA stride；保留VA不算物理副本。

## 实际执行与所有权

`SharingPool` 仅在独立测试包中适配四 participant；生产 manager 未改。
independent 为两个 P_i/D_i session；shared 为 `[P_a,P_b,D_a,D_b]`，贡献
`[S_P,0,S_D,0]`。每个进程保留自己的 handle 和设备地址，两个成员都直接
调用生产 `torch.ops.npu.unidex_copy` 访问 owner，owner 不转发 copy。

join 后核对贡献、stride、层首尾映射、四份 probe。SDK Python 没有公开 BM
reach 查询，记录 group callbacks，并在所有设备 drain 后让两名零贡献 follower
分别 leave/rejoin，要求其他成员在本次操作开始后依次观察到 LEAVE/JOIN、
映射保持稳定、probe仍正确；最初join的旧通知不能满足重入检查。
这提供成员状态与访问的实际证据；不把参考SDK源码或普通join当成全部通过。
物理owner的 `/proc/self/maps` 和 `numa_maps` 驻留页记录用于确认落点。
完整模式若观测到其他节点的驻留页，立即失败。两份记录均匹配的`/dev/davinci_manager`驱动映射
可能不提供任何节点页计数；这种情况记录`unobservable_driver_mapping`，保持
`placement_verified=false`，继续采集完整矩阵和trace，不将缺失计数当成节点错误。
其他无法确认映射的情况仍立即失败。SDK分配日志（如`HalMemCreate ret:0 numa:0`）
保留为补充证据，不自动替代物理落点的独立观测。

固定地址的 inputs/source/output 支持空mask捕获后重放有效数据，覆盖slot/index
切换、padding和重复replay。每次计时前后独立核对完整selected结果；写入由两个
reader分别核对完整写入区域、所有slots相同区域及首尾guard。BF16已知pattern
编码side/layer/slot/token/feature，写入改变全部feature，漏写后半行也会被发现。

写分工对比 token、layer、feature，含1／17／2049行与奇数尾部；feature用两个
576-byte连续span，始终调用同一个生产operator。layer单层只有一名writer；
token单请求单行也只有一名writer，报告保存实际有效单元和字节数。
等逻辑写入：独立合计2L，共享合计L。等copy字节实验：两者均2W，共享写互斥
扩大范围，单独报告，不用该诊断的区域大小声称容量去重率。

read对照每个reader始终完整读selected KV；包括P、D、mixed、连续／离散索引、
1／16个有效请求及单reader的owner/follower互换。有效历史超过2048，带宽case
有完整有效top-k；zero-valid另列为控制项。
每个case先测P侧两个rank，再测D侧两个rank；另一侧保持加入、完成准备后
等待，测量期间不提交设备操作。因此D侧的本地D／远端P对照不会混入P侧
另外两个reader的争用。单reader诊断同样只保留指定的一个访问者。

纯copy用预计算device routes；metadata/HBM hit/BM miss/refill组合另调用现有
生产 `SparseKVCacheManager.materialize_selected_kv` 与 `SparseKVCopy`，固定0%、
50%、100%命中率。每次操作前恢复fixture cache，避免replay后全命中；恢复不计入
该操作device timer，但计入同机host envelope，报告明确标注。
BM miss沿用P后D串行顺序。copy payload包含实际BM/HBM/refill/slot-map请求字节，
另列BM payload，不声称这是实测总线流量。

`write_then_read` 先写入、经本机两个rank的HCCL发布，再从BM读取包含current的
完整selection；独立与共享模式都包含这次collective。`write_with_history`
使用相同selection，改由本地current与历史读取完成selected，BM写回并行执行。
两者分别记录selected-ready与包括writer完成的完整操作时间。

current_serial/current_parallel固定写回策略，比较本地current与历史copy填充同一
selected buffer的时间。初始化先完成，目的mask互斥；selected-ready只等所需
current/history，最后的操作完成事件才等writer。Graph的selected-ready通过
外部事件交给观察stream记录：record与wait分别只capture一次，重复回放观察
graph；包括warmup/check在内，每次实际调用都消费信号，再允许下一次复用。
旧event后端在main上reset，value-wait后端由wait消费并清零。每次在graph外
创建新计时事件，因此结果包括观察stream调度延迟，不读取内部capture事件
的旧时间戳。该机制仍须在部署SDK上实测，版本不兼容会直接失败。
观察等待在producer（含HCCL）提交后入队，避免观察stream复用已缓存的HCCL
stream时将通信堵在等待之后。selected时间包含host提交与观察调度延迟，是就绪
延迟的观测上界，不能单独据此断言writer是否发生重叠；另用独立trace核对。
依据：[HCCL stream分配与缓存](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/distributed/ProcessGroupHCCL.cpp)、
[stream池复用](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/core/npu/NPUStream.cpp)。
接口依据：[torch_npu events](https://github.com/Ascend/pytorch/blob/master/torch_npu/npu/streams.py)、
[ExternalEvent wait/record实现](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/core/npu/interface/AsyncTaskQueueInterface.cpp)。

完整模式另做延迟writer测试：本地selected先可用，writer完成后接入本机两成员
HCCL，再立即在设备上读取完整共享新行，之后才做host rendezvous。eager在设备
drain后用`Event.recorded_time()`读取同一设备的原始微秒时间戳，计算ready→selected、ready→writer设备计时，要求延迟writer
的完成时间严格晚于selected。host晚调度时query看到两者都完成不能判定设备先后。
Graph验证完整数据并输出独立trace，不用capture内的旧时间戳判定重叠。
此组件通信依赖不证明02真实模型TP hook已经接好。默认用512次矩阵乘产生延迟，
延迟发布fixture先提交selected分支，再提交writer延迟循环；二者仍只依赖共同ready，
不增加selected→writer设备等待。否则eager主机逐个提交512次mm的时间可能消耗
整个设备延迟，selected尚未提交writer就已完成（run-8 D member-1间隔-0.230ms）。
该调度仅用于publication+delay检查，正常性能矩阵的提交顺序不变；此fixture只证明
selected不必等待writer及发布后的读回，不将人为间隔用作性能收益。
若设备仍没有正向间隔，检查保持失败；不自动重试到成功，也不放宽断言。

run-6-full首轮shared性能矩阵完成后，D member-1在旧host query断言失败。
设备计时修正后可先用`--diagnose-publication --timeout 600`和新run-id重测：
只执行一轮independent/shared的初始化、映射/重入检查、KV staging及延迟发布
eager/Graph、两种variant和独立trace，不执行性能矩阵。保留默认delay-matmuls=512，
先区分host观测误报与设备间隔确实不足。scope为publication_diagnostic，成功仅为
DIAGNOSTIC_PASSED，不作为三轮性能或NUMA完整验收；与其他diagnose及smoke互斥。
只对eager发布检查启用事件计时；需要torch_npu提供
[`Event.recorded_time()`](https://www.hiascend.com/document/detail/zh/Pytorch/700/apiref/apilist/ptaoplist_000248.html)。
每个worker的delayed-publication.json逐项保存原始timestamp_us、selected_ms、writer_ms和
writer_minus_selected_ms（eager）；后者不大于0时仍失败，失败前保留时间差。
summary.json包含成功的delayed_publication记录，Graph先后关系仍需trace核对。

全部worker停止提交、完成所有相关streams后才共同确认drained，再leave/destroy。
异常会中止其他worker的阶段；只有四方确认drain才正常释放。超时、设备错误或
强制停止均返回非零并保存失败／cleanup信息，不作为安全释放成功证据。
父进程仅清理本次拥有的process groups。

## 报告与验收

日志根目录固定在 `/home/cryang/log/numa-sharing/<run-id>/<prefill|decode>/`。
回传两侧目录，至少包括：

- `summary.json`：scope、代码SHA与源文件hash、物理贡献、逐case比较、原始组样本。
  P另外收集 `decode-summary.json`。服务验收和用户确认固定保留pending。
- `round-N/<mode>/member-M.log`、`member-M.command.json`、配置JSON。
- `round-N/<mode>/member-M/report.json` 与 `samples.json`：SDK/driver/kernel、
  NUMA驻留页、mapping、成员事件、每rank设备计时、字节数、完整性及close结果。
- `delayed-eager-trace.json`、`delayed-graph-trace.json`：独立profile；不进入正式样本。
- 失败时的 `failure.json`、worker traceback 和 `cleanup_error`。

同机两rank用host monotonic计算 `max(done)-min(start)` 和启动偏差，不相减
跨NPU event timestamps或跨主机时钟。记录每次device duration，报告rank/group
P50/P95和逻辑／copy／BM速率。两rank活动样本要求host执行窗口重叠且启动偏差
不超过较短device batch的10%；不满足的原始样本保留、排除出比较，每轮至少
一条对齐样本，否则失败。该条件证明批次对齐；精细设备重叠仍结合trace审阅。
mixed/scattered单请求读取另将同负载的双reader和owner/follower单reader关联，
报告 `dual_reader_efficiency`：实测双reader BM带宽除以两次实测单reader带宽之和。
分母只用于效率诊断，报告中的双reader吞吐始终来自同时运行的那组样本。

完整成功输出 `COMPONENT_CHECKS_PASSED`，smoke仅为 `SMOKE_PASSED`，退出码0。
完整模式若任一侧任一owner的驱动映射没有节点页计数，完成测量、双侧报告收集和清理后，
两侧均输出`COMPONENT_RESULTS_PENDING_NUMA`并返回退出码2；`summary.json`包含
完整结果、`numa_placement_verified=false`和`numa_acceptance=pending`。
这表示数据采集完成、NUMA证据仍待确认，不能作为完整验收通过或解锁02。
smoke保留未验证的NUMA状态，但不执行完整模式的落点门禁；smoke退出码0不代表完整验收。
内容错误、版本／配置不一致、NUMA／mapping／成员检查失败、缺样本、无法对齐、
timeout或cleanup错误返回退出码1。组件不引入逐case的3%门槛；用户审阅读写瓶颈、
selected-ready、去重和可用候选后，才按verification.md确认关闭01并解锁02。
TP16吞吐与ITL三项3%正式门槛仍由03验收。

## 进度显示与续跑

父进程每10秒从本机两份worker日志增量读取BEGIN/PASS，显示总组进度条、当前
round/mode、两名worker较慢者的已完成用例数、各自当前阶段及本次耗时。
百分比按用例计算，不是剩余时间预测；setup/staging、发布和清理也会显示，组关闭
前最多99%。不在设备计时区间插入进度I/O。

若run-8-full前两轮四组都成功，可在两侧使用新run-id并添加
`--resume-from run-8-full --resume-round 2`。其余参数保持
`--block-dims 24 48 --rounds 3 --timeout 3600`，角色/IP/NPU/NUMA不变。
入口先核对旧plan、旧source指纹、每worker的硬件/负载配置、成功状态、closed、
发布读回、非空trace和完整性能样本，然后两侧互认复用列表，才启动第三轮independent/shared。
旧报告按member核对SDK/驱动/CANN/内核/设备/hostname/CPU亲和性；新worker在BM
初始化前对照旧environment，任何差异都拒绝混合。
仅接受相同代码或已审阅的622c93f88e精确源指纹迁移：这次只修发布fixture，
常规性能测量未变。迁移同时锁定当前实现的代码指纹，未来更改计时实现后需要重新
审阅兼容性；不接受任意旧版本、失败组或缺失样本。

新目录保留复用report副本，resume.json及summary.resumed_from标注源目录和版本；
每组reuse.json指向原始trace/log。不要删除源目录。汇总包含复用的前两轮和新第三轮，
不把失败的第三轮样本并入，也不宣称这是一次连续运行。NUMA未确认仍exit2，
trace人工复核和用户验收仍保留。入口拒绝配置不一致，不用手工拼JSON绕过。
