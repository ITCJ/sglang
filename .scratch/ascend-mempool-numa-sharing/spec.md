# MLA NUMA 共享 mempool

**Type:** spec
**Status:** ready-for-agent
**State:** open

**当前阶段（2026-10-14）：** 用户审阅 TP1/2/4 的 C/D 结果后明确接受01完成。
01已关闭，02可以开始，03仍等待02。默认部署每个 NUMA 节点安排两个 TP ranks；
先完成 NUMA 分配，再按 `mempool_share` 决定是否合并物理池。分组数量和大小由
实际分配结果生成，两个 ranks 是部署默认值，不是实现上限。
全部组件数据的 `numa_verified=false` 原样保留；关闭01不等于证明物理页落点。

2026-10-14 用户批准新的组件实验范围：TP1/2/4、独立集中/独立分散/共享七种布局，
固定池形状，聚焦 C 大块连续分工写入和 D 相同索引 top-k=2048 完整读取。
执行合同与逐机命令见 [NUMA_CD.md](../../ascend-mempool-test/NUMA_CD.md)，
批准前的设计图见 [C/D 实验设计](visuals/experiment-v2-plan/report.md)。
这一轮替代旧性能矩阵作为下一轮采集任务；不将旧矩阵结果拼接为新实验的样本，
也不将组件测试通过视为服务集成或 NUMA 硬件验收。结果与关闭依据见01最新记录。

2026-10-12。用户逐项确认目标，并要求第一阶段先比较独立池／共享池的双 rank
并发读写性能，第二阶段细化并行路径，第三阶段保持原验收要求。
据此发布[三阶段交付计划](delivery-plan.md)；`ready-for-agent` 不代表已实现或验收。
同日用户明确确认“初步设计完成了”。设计讨论收束，后续从01组件读写对照实施，
未决硬件问题由对应实验回答；实现与NPU验收状态仍按各 ticket 记录。
代码事实与讨论依据见[设计记录](design.md)，同步决定见
[ADR 0001](../../docs/adr/0001-mla-shared-kv-publication.md)，具体对照矩阵和
并行依赖见[执行设计](execution-design.md)。

## Problem Statement

当前 Ascend MLA mempool 为每个 TP rank 保存完整 compact KV。TP16 时同一侧
存在 16 份逻辑副本；将两个 rank 的分配放到同一 NUMA 并不会使它们共享数据。
GLM-5.2 每张 NPU 的 Index K 容量已达到用户报告的约 3M token，长期 KV 副本
带来的 DRAM 消耗与重复写入开始妨碍容量增长。

需要根据 NUMA 和 NPU 访问关系保留能提供带宽收益的副本，同时消除组内重复
存储和写入。首版优先验证去重、正确性与同负载性能，实际 2T 机器各 NUMA
可分配的 BM 容量留待测量，不要求配齐 3M token 或达到总内存容量上限。

## Solution

在当前 TP16、DP1、PP1、CP1、BF16 MLA 的正式 PD mempool 路径上，先为各 rank
确定本机 NUMA，再按共享开关创建存储。`mempool_share=0` 时每 rank 一个池；
`mempool_share=1` 时，同主机、同 NUMA、同一逻辑 compact KV 的 ranks 共用一个池。
P/D 使用相同逻辑 rank 分组，但分别指定本机 NUMA；P 存 prompt KV，D 存 decode KV。
默认部署每节点两个 ranks，TP16 对应每侧8份 replica；其他分配按实际组数统计。

组内所有 ranks 都直接映射并读取完整 replica，写入工作在 ranks 间分工，
每个逻辑写入单元仅写一次。分工粒度由性能测试决定，可比较 token/block、layer
和 feature；完整物理 allocation 的 owner 与 writer 身份分离。

decode 本步新 KV 直接从各 rank 的本地计算结果供给 attention；历史 KV 读取
本地 sparse cache 或共享 mempool。利用 attention 后已有的 TP 通信覆盖共享
写入的完成次序，首版不新增独立跨 rank barrier。比较同 producer stream 与
独立 write stream 两种调度；独立 stream 尽早提交，与 indexer、历史 KV 获取
及 attention 的无依赖部分重叠。用户进一步确认允许同一步内滞后一层：L 层
写回与 L+1 层 attention 并行，在后者的已有 TP 前等 write(L)，末层收齐尾部。
当前 KV 填入 selected buffer 与历史 UniDexCopy 独立并行提交，attention 和
refill 汇合所需来源的完成事件；selected-ready 不等待 BM 写回。

服务接入前的组件实验已扩展到 TP1/2/4，并完成独立集中、共享集中与独立分散
配置对照。等逻辑写入与等 copy 请求字节数分开；所有 readers 各读完整 selected KV。
这些结果支持默认双 rank 的部署选择，真实服务的收益仍由02、03验证。

在相同模型、输入、并发、Graph 与逻辑 KV 容量下，compact KV payload 默认从
16份降至8份；实测并分别报告对齐、probe等开销后的物理分配。多轮对照中
decode 吞吐、ITL 中位数及 P95 的回退均不得超过 3%。

## User Stories

1. 作为部署者，我希望先配置各 rank 的 NUMA，再切换共享开关，以便保持部署与池数量独立可控。
2. 作为部署者，我希望为 P/D 的同一 rank 组分别指定 NUMA，以便适配两台机器。
3. 作为部署者，我希望每个共享组在每侧只有一份完整 compact KV，以便减少 DRAM 副本。
4. 作为部署者，我希望多个 rank 直接访问共享存储，以便共同利用 NPU 读写带宽。
5. 作为维护者，我希望物理分配者与 writer 分离，以便写入策略不决定内存是否重复分配。
6. 作为开发者，我希望每个 KV 写入单元只由一名 writer 写入，以便避免重写和缺写。
7. 作为开发者，我希望按实测选择分工粒度，以便兼顾 prefill 与单步 decode 的不同形态。
8. 作为模型使用者，我希望本步新 KV 直接供给 attention，以便避免等待 peer 写入后读回。
9. 作为模型使用者，我希望历史 KV 只在完整可读后被共享，以便跨步结果正确。
10. 作为部署者，我希望复用已有 TP 通信，以便共享存储不增加逐层跨 rank barrier。
11. 作为部署者，我希望共享组、模型布局和身份在请求准入前匹配，以便错误配置明确失败。
12. 作为开发者，我希望每个进程持有自己的稳定设备映射，以便 NPU Graph 使用合法地址。
13. 作为开发者，我希望 Graph replay 可更换请求、slot、长度和索引，以便同一图服务后续请求。
14. 作为模型使用者，我希望 padded rows 和无效索引不访问真实 KV，以便图 padding 不损坏数据。
15. 作为部署者，我希望 P/D 的容量可以不同，以便按 prompt 和生成长度分配内存。
16. 作为部署者，我希望容量不足时保留明确的准入和等待行为，以便共享不会破坏请求所有权。
17. 作为开发者，我希望共享 slot 保留到全部 readers/writers drain，以便回收不会覆盖仍在使用的数据。
18. 作为开发者，我希望请求 attempt、generation 和 session 仍隔离迟到消息，以便 slot 复用安全。
19. 作为部署者，我希望 Index K 和必要的 handoff 继续可用，以便 compact KV 去重不改变模型语义。
20. 作为维护者，我希望当前独立 rank 模式继续可用，以便做性能对照和部署回退。
21. 作为验证者，我希望直接比较真实共享 KV 内容，以便发现正常输出不能暴露的复制错误。
22. 作为验证者，我希望同负载比较吞吐及延迟分布，以便容量收益不会掩盖性能回退。
23. 作为验证者，我希望区分 payload、实际物理贡献和虚拟地址空间，以便确认去重确实减少 DRAM。
24. 作为部署者，我希望在可运行的小容量完成首版验收，以便可分配容量调查不阻塞共享设计。
25. 作为验证者，我希望每台主机一个入口自动完成测试与清理，以便重复实验可比较且失败明确。
26. 作为维护者，我希望每阶段硬件验收后才解锁依赖工作，以便组件问题不会带入后续服务集成。
27. 作为验证者，我希望先比较两个独立池和一个共享池的双 rank 同时读写，以便在模型接入前发现带宽瓶颈。
28. 作为验证者，我希望读取对照保持每个 rank 的完整 selected KV，以便不通过少读数据伪造收益。
29. 作为验证者，我希望分开比较等逻辑写入与等 copy 请求字节数，以便区分去重收益和共享池争用。
30. 作为模型使用者，我希望共享写入尽量与 indexer、历史读取和 attention 重叠，以便写入不会无谓延长关键路径。
31. 作为开发者，我希望时间线证明实际重叠并量出剩余等待，以便不把增加 stream 当成性能收益。
32. 作为模型使用者，我希望新 KV 填入 selected buffer 与历史 UniDexCopy 并行，以便本地供给不会串行阻塞历史读取。
33. 作为模型使用者，我希望写回可覆盖同一步下一层 attention，以便扩大重叠窗口，同时在 step 结束前完成全部写入。
34. 作为部署者，我希望默认每节点两个 ranks，并可通过 NUMA 分配改变组大小，以便复用同一实现。
35. 作为部署者，我希望关闭共享后每个 rank 都恢复独立池，以便沿用现有基线和回退方式。
36. 作为维护者，我希望启动前验证完整分组与容量，并输出最终映射，以便发现跨主机节点重名和 P/D 成员不一致。

## Implementation Decisions

### 首版范围与配置

- 沿用当前 TP16、DP1、PP1、CP1、BF16 MLA 主模型非投机配置；GLM-5.2 为主验收模型。
- 默认部署为每节点两个 ranks；分组实现接受实际成员数，至少验证1/2/4成员与
  不均匀分配的配置行为。它们是共享组大小，不要求把主验收模型改为 TP1/2/4。
- 先解析每个 rank 的本机 NUMA，再构造共享组。`mempool_share=0` 产生单成员组，
  `mempool_share=1` 按同主机、同节点、同 TP 模型实例及兼容 KV 身份归组。
  组内一个物理 owner，其余成员各保留自己的 SDK handle 与设备映射。
- 共享开关是启动时配置。为保留旧启动行为，未设置时保持独立模式；新的共享
  部署模板显式启用共享并采用双 rank 默认布局。改变开关或分组需要重启，
  不在请求执行期间在线测带宽、迁移 slot 或重建池。
- 2026-10-22用户调整：NUMA节点列表按每台主机内rank升序连续均分，替代原轮转。
  TP4配`0,2`得到`0,0,2,2`；不能整除时前面的节点多分一个，节点多于ranks时
  只使用前若干节点。显式逐rank映射仍支持自定义布局；分配规则独立于共享开关。
- 每个 rank 恰好归一组，组不跨主机或 NUMA。P/D 节点编号可不同，但逻辑成员
  分组与共享模式必须匹配；启动输出最终映射并在分配/准入前校验。
  共享开启时未知/非法节点、不兼容 layout/identity 或不匹配的分组明确失败，
  不把默认节点占位值当成同一节点，也不静默恢复为全量副本。
- 首版保持现有固定 slot 容量模型、边界与索引范围检查。容量是配置参数，
  不在启动时按总内存均分或自动尝试占满每个 NUMA。

### 物理存储与映射

- 组大小为 g 时，P/D 配对共2g个 BM participants；每侧一个完整物理 allocation
  owner，其他 g−1 个成员为零贡献。两侧容量可不同，预留 stride 在组内一致。
  默认 g=2，g=1 与 g=4 使用相同合同；完整副本数量按实际共享组数计算。
- 每个进程独立将 owner 的地址映射为本进程可用的设备地址；组内 ranks 直接读写，
  allocation owner 不承担数据转发。单 allocation 保持每层完整视图连续。
- pool/group identity、BM participant、P/D 角色、allocation owner 与 writer 分工分别表达。
  store、端口和 probe 身份按共享组重新规划，避免沿用双 participant 假设覆盖其他成员。
- 零贡献 participant 的正常加入、查询状态、共享访问与关闭必须在实际 SDK 上验证。
  若该候选不可用，先记录证据并调整分配方案；不静默恢复全量副本或进入服务集成。

### 数据路径与完成次序

- 写入分工覆盖全部有效 KV，分片不重叠；其覆盖范围由明确的 owner 规则决定。
  首版不把固定的按 token、layer 或 feature 策略提前写死为性能结论。
- P 继续保留 native HBM KV；prompt KV 分工写共享 P 存储，完整可读与既有
  Index K/state/metadata handoff readiness 均满足后，D 才进入 decode。
- D 本步新 KV 从本 rank 的临时计算结果填入 selected KV，排除对应的 BM miss。
  历史 token 延续本地 cache 命中或 P/D BM 读取；未被 top-k 选中的新 KV 也必须写入。
- current-fill 与历史 BM UniDexCopy 在不同 streams 独立提交，写 selected 的
  互斥位置，仅在 attention/refill 消费端汇合。比较独立 current stream
  （与历史 hit/miss 都无相互等待）和复用 hit stream（current 与历史 HBM
  hits 顺序执行，仅与 BM miss 并行），按实际 selected-ready 与服务耗时选择。
  公共 ready 事件不能包含 current-fill 或 BM write-done；selected 初始化须先完成。
- 保留同 producer stream 作为调度对照，首版允许独立 write stream。compact KV
  完成 norm、RoPE 和所需重排后即可提交分工写入；不能依赖尚未产生的 top-k。
  尽早提交的路径要替换原 backend 写入，避免一层被写两次。
- 独立 write stream 等本地源 KV 与写 metadata 就绪；当前 token 本地供给、
  历史 hit/miss 和 attention 不因本步 BM 写入而额外等待。原有各自的数据依赖保留。
- 比较同层完成与一层滞后的写回：普通层 TP(L) 前仅等待本 rank 的
  write(L−1)，不等待 write(L)；首层无前驱，最后一层 TP 前收齐剩余写回。
  写回可覆盖同一步下一层 attention，不跨 decode step。等待具体层事件，
  不用等待整个 writer stream 的方式误把当前层写回也带进来。
- 对应 TP collective 必须覆盖共享组全部 writers；L 层的本步 KV 在
  TP(L+1)（末层为尾部 TP）完成相关设备依赖后，才可作为共享历史使用。
  本层 TP 不能被当成本层写入已发布的证明。不加 TP barrier 或逐层 host synchronize。
- 使用有界 source/metadata staging；单执行 stream 下两组交替作为最小候选。
  L+2 复用 L 的一组前，write(L) 及本地 current 消费均须完成。普通 scheduler
  overlap 保留，多个 CPU pending receipts 不代表可覆盖仍在使用的设备 buffer。
- producer indexer 层与 shared-topk 层分别测量：后者没有 indexer 计算窗口。
  只在实测收益成立时采用相应调度；所有候选在下次同层历史读取前完成发布，
  并在本 step 内收齐写回，不能省掉必要依赖。
- P 侧允许 BM 写入与 native HBM、计算及 Index K/state handoff 的无依赖部分
  重叠；仅在所有 prompt 分片完成后发布 KV_READY，D 仍同时等待 native readiness。
  pending 的源 buffer 必须受现有 forward/chunk 边界约束，不形成无界队列。
- 区分实际 writer 的写入覆盖检查、本地新 KV 可读范围及共享历史 KV 可读范围。
  不把每个 rank 的分片计数伪装成完整 prefix，也不以输出 token 数代替真实 KV。
- current KV 供给、miss、cache refill、metadata 复用和 drain 使用固定、受生命周期
  保护的 buffer；current-hit-miss 的 destination masks 互斥，refill 等待所有相关
  来源完成。write stream 不得与既有 miss/refill 共用会被覆盖的 scratch。
  首版不引入跨多个 decode step 拖延的写入队列。

### 控制与生命周期

- 尽量沿用现有 P_i/D_i 控制消息和全 TP 一致 acquire/release；共享存储身份额外参与
  握手，证明同组控制 peers 指向同一份物理 replica。
- 共享 slot 只有一个逻辑 request owner；所有参与 rank 的 slot/generation/attempt
  必须一致。allocation owner 不得依据自己的局部完成状态提前回收或销毁共享存储。
- 正常结束、取消和关闭都必须覆盖共享组所有已提交读写；无法证明 peer drain 时，
  按现有 fail-stop／保留资源合同处理。首版不增加自动接管或容错恢复策略。
- 共享改动的生命周期回归属于本需求；原后续票据的完整容量、取消和故障矩阵仍保留。

## Testing Decisions

### 两个主要验证入口

1. **共享 KV 组件入口：** 驱动真实 BM 映射、写入、稀疏读取和 NPU Graph，
   用独立的已知 KV 内容校验结果。01已有 TP1/2/4 对照与用户验收；02复用入口
   验证生产分组接入涉及的改动，不为未改动的 copy 路径重跑全部旧矩阵。
   组件结果不能代替 TP16 真实模型验收。
2. **真实 PD 服务入口：** 使用相同权重与请求，在原独立 rank 模式和共享模式
   下验证生成、Graph、释放、DRAM 去重和性能。复用已有自动服务验收框架。

CPU 检查覆盖组映射、地址布局、writer 覆盖、边界、身份和生命周期错误；
验证外部行为与不变量，不复制生产实现生成自证结果。

### 必须有的硬件证据

- 一个 owner 的物理区被组内所有 NPU 正确读写，分工无重叠／缺失，非写入区域不变。
- 零贡献参与者的 join、状态查询和关闭行为明确；P/D 不同贡献大小可用。
- 本步新 KV、过去 decode KV、远端 prompt KV，以及 cache hit、P-only、D-only、
  mixed、zero-valid、padding 等情况取得正确结果。
- Graph capture/replay 在更换请求、slot、长度和索引后仍正确；带 writer 延迟的
  对照证明本步本地读取和后续共享读取的顺序。覆盖独立 write stream 的设备
  事件、metadata/source 复用与最终 drain，无需新增跨 rank barrier。
- current-fill 与历史 UniDexCopy 使用相同 selected buffer 的互斥目的位置；
  覆盖不同位置的新 token、无选中新 token、padding 和重复 selected 位置，
  selected-ready 及 refill 等待正确；不能只测输出文本。
- 跨层写回覆盖首层无前驱、中间层等待前一层、末层收齐、交替 staging 复用、
  连续异步 forward 和中途 drain。尾部 flush 必须位于捕获模型内部的最后 TP
  之前，不能只写在 Python forward-completion 回调中。
- timeline 分别证明 current-fill／历史 UniDexCopy 并行，以及 write(L) 与
  attention(L+1) 的重叠；报告 selected-ready、下一层 TP 前剩余等待、尾部
  flush、小 batch 与 producer/shared-topk 层差异，不能以 stream 数量验收。
- 短 greedy 输出与当前正式 mempool 对照，覆盖大于 index_topk 的实际稀疏读取；
  GLM-5.1 保留快速 Graph 回归，避免破坏既有 MLA 支持。
- 检查实际模型路径中组内 compact KV 的数据等价性；replicated projection 的
  结构不能代替量化、batch 和 Graph 配置下的真实内容验证。
- 正常释放与 slot 复用覆盖全部读写；共享改动相关的错误／取消不会提前重用存储。
- 默认 TP16/8 组及自定义分组报告实际 payload 副本数、各 participant 物理贡献、
  对齐/probe 开销和 NUMA 证据。请求节点与实际页落点分别报告，证据缺失保留
  unverified，不依据带宽反推已验证落点。虚拟映射大小不算物理副本。
  用户已接受该限制关闭01；02/03独立提供真实服务验收，不自动重开09容量调查。

### 性能比较

第一阶段组件对照已完成并经用户接受；以下口径以默认双 rank 组说明，组大小
为 g 时推广为 g 个访问者，完整实验合同与数据见01：

- 独立池：相同 NUMA 上两个 pools，两个 ranks 各写完整逻辑 KV；共享池：一个
  pool，两个 ranks 同时写互斥分片，合起来写一份。比较同一逻辑任务的完成时间。
- 另做等 copy 请求字节数对照：两个 ranks 在两种模式中各写相同字节数，目的地址
  互斥。用于测共享 allocation 的争用，明确区别于“副本写入减半”的实际收益。
- 两种模式下两个 readers 都各读完整 selected KV 到自己的 HBM，保持有效 token
  数、索引和输出字节数相同。覆盖本地 D 历史、远端 P prompt、P/D mixed 与
  历史读／新 KV 写重叠；零有效项仅作正确性对照，不计为有效读取带宽。
- 固定 NPU/NUMA、SDK、copy 实现、数据规模、索引分布、暖机与测量方法；分别测
  eager 与 Graph、小粒度 decode 与 bulk。单 rank 结果只作并发效率诊断。
- 报告每 rank 延迟、组完成时间、逻辑 KV 速率、有效 copy 字节数／时间及 P50/P95，
  保留原始样本。copy 请求字节数不能冒充包含缓存与协议开销的物理总线实测流量。
  不把异步 enqueue 时间或单 rank 时间作为双 rank 完成时间。

主要基线是同一 NUMA 分配、共享开关关闭的16个独立 rank-pair；默认共享目标为8组。
组数或 NUMA 分配改变属于另一配置，对照中不同时改动开关与分配；四成员组
作为可配置能力和容量/带宽取舍单列，不能混入双成员默认值的正式验收。
两侧使用相同逻辑 KV 容量、模型权重、输入、输出长度、并发、Graph、量化及软件环境。
覆盖单请求与多请求、短序列和大于 index_topk 的长序列，报告实际 cache hit/miss
条件。先用组件筛选写入粒度与读写并发方案，服务接入阶段分别比较 selected
填充串行／并行、同层写回完成／滞后一层，第三阶段完成策略选择和正式 3% 验收。

暖机后采用多轮交替对照，每个代表负载分别比较；噪声足以影响 3% 判定时追加
成对样本，不用跨负载均值掩盖某类负载回退。保留原始流式 token 时间记录，
区分 ITL 与按请求均摊的 TPOT。

- decode throughput：共享模式不少于基线的 97%。
- ITL 中位数：共享模式不高于基线的 103%。
- ITL P95：共享模式不高于基线的 103%。

物理容量扩大另测，并报告各 NUMA 实际可分配容量与环境条件。组件 copy 带宽
不能代替服务性能；正常文本输出不能代替共享 KV 内容比对。3% 是服务最终验收
容差，不额外推导成每个 copy 大小的微基准硬阈值。组件阶段需审阅读写瓶颈后再
进入服务接入，性能问题不能仅凭去重成功而忽略。

### 执行与交付

测试入口自动完成环境检查、启动、readiness、请求、结果核对、报告收集和自身
进程清理；多机每台主机／容器各一条命令可接受，随后自动协调。
NPU 日志与报告写入 `/home/cryang/log/`，失败返回非零，区分组件 smoke 与
完整验收。NPU 由用户运行，每阶段经用户验收后才解锁依赖工作。

## Out of Scope

- GQA、非 MLA KV 布局、DP/PP/CP 扩展、MTP／投机推理。
- P/D 不同逻辑分组、跨主机或跨 NUMA 合池、在线自动选路和动态重建/迁移池。
- 对任意组大小的性能保证；默认双成员配置以外的性能分别报告。
- Index K HBM 去重、KV 量化、跨 rank 读取合并或 multicast。
- 固定 slot 改为分页／动态容量 allocator、硬顶 3M token 或 2T 总内存。
- 驱动／BIOS 修复、SDK 自动恢复、共享存储 owner 接管。
- 原 ticket04 的 P/D miss 并行读取及后续完整故障／精度矩阵。

## Further Notes

用户确认先完成 NUMA 共享，再衔接原 mempool ticket04；此前已验收票据保持关闭。
本规格不会把公开 SDK 源码或 Mac 检查当作 NPU 验收，也不会自动恢复已延期的
NUMA 分配故障调查。分阶段依赖以[交付计划](delivery-plan.md)及独立 tickets 为准。

## Comments

2026-10-12：按用户对三阶段的反馈定稿：01 增加独立／共享池双 rank 同时写入和
读取的性能对照；02 细化并行设计并比较调度；03 维持已确认的去重和 3% 验收。
工程实现及 NPU 结果尚未产生，所有 implementation tickets 保持 open。

同日补充：用户指出 current KV 仍需 copy 到 selected buffer，要求与历史
UniDexCopy 并行，并确认写回可覆盖“同一步的下一层 attention”。据此细化
两处独立汇合：selected-ready 等当前读取来源，写回在下一层已有 TP 前完成，
末层收齐。本轮仅调整设计与验收，没有生产实现或硬件结果。

同日最终确认：用户通过 `ask-matt` 明确“ok，我觉得初步设计完成了”。当前
规格与三票作为初步设计基线；继续实现时以01为入口，无需重复已确认的需求
问答。硬件结果若要求改变方案，记录证据并更新相关设计，不将设计确认视为
任何 implementation ticket 已通过验收。

2026-10-14：用户审阅 cd-v1-resume2 结果后明确“ticket01可以视作完成”，并决定
默认每 NUMA 两个 TP ranks。共享由 NUMA 分配结果加0/1开关决定，替代旧“只支持
显式八个双成员组”的限制。01按用户验收关闭，NUMA原始标记不变；02解除阻塞，
03仍依赖02。此轮更新规格与安排，尚未实现生产共享配置或执行真实模型验收。
