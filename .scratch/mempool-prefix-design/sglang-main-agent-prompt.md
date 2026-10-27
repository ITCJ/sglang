你正在一个新的 sglang-main 仓库中。请用中文，基于当前 checkout 的真实代码，帮助我理解 SGLang 主线 GPU 实现的 prefix cache、radix tree、HiCache、Mooncake，以及 router / worker scheduler 如何协作。任务是源码研究与框架教学，讲清当前实际实现的原理、调用链、启用条件和限制。

## 研究方法

先报告分支、commit、仓库结构及相关配置入口。以本地实现为主要证据，官方文档和上游源码补充；说明证据版本。若本地 checkout 不能证明“最新主线”，明确该边界。

从 scheduler 的 cache 初始化/registry 查起，确认实际使用的缓存类及选择条件，再追踪实现。仓库可能包含 `RadixCache`、`HiRadixCache`、`UnifiedRadixCache`、不同 tree core 或 external linker；按真实调用链决定讲解重点。

我写的“mooncache”请先核对项目名；若实际指 Mooncake，就使用 Mooncake Store 的正式名称，并区分 Store 与用于 P/D 数据搬运的 TransferEngine。

router / gateway 也按实际目录、依赖和版本定位。若相关实现已在独立上游仓库，查对应官方源码，注明边界。用实际 producer→consumer 调用链证明职责；仅找到事件定义或一个类，不足以说明功能已经接入。

## 请依次讲清这些问题

### A. L1 prefix cache 与 radix tree

- 真实 KV tensor、allocator、request→token 映射与 radix tree 各存什么、由谁拥有。
- 节点的 key/value 是 token、page、索引还是 KV 数据；一个节点能否跨多个 page。
- longest prefix match、insert、split、引用保护、eviction 的实际过程；split 是否复制 KV。
- 命中后如何把 cached indices 接到请求，并只为未命中部分分配/计算。
- unfinished / chunked / finished request 何时进入缓存；活动请求与可淘汰缓存如何共存。
- page_size>1 时如何处理尾页、共享与追加写，是否需要 copy-on-write。
- 最后采样 token 尚无 KV、全命中仍可能需要一次 forward、logits/logprob 等边界。
- 用一组短 token 序列，画出插入 A、B 复用、分叉、请求结束和淘汰前后的树及页映射。标注示意 page_size，区别真实配置。

### B. HiCache 与 Mooncake 的统一视图

- L1 GPU HBM、L2 host memory、L3 storage 的实际作用域；哪些可跨实例共享。
- 一个前缀可同时位于哪些层，tree 如何记录本地状态，外部对象如何发现。
- 分别追踪 L1 hit、L1 miss/L2 hit、L1/L2 miss/L3 hit、全 miss。
- 区分 match、allocate、prefetch、load-back、write-through/write-back、evict；谁发起、谁完成、何时可计算。
- storage 的 page hash 如何包含父前缀与 namespace；模型、adapter、cache salt、layout、rank/shard 如何影响兼容性。
- L3 hit 是只看主 KV，还是必须包含模型所需的附属状态；中间缺页、部分恢复和超时如何限定可复用前缀。
- cache mode、buffer mode、direct linker 等如果在当前代码存在，给出主要差异和启用条件，避免把所有路径合成一条。

### C. GPU 主线的 layer pipeline（重点教学）

我不熟悉分层流水，请先给具体时间线，再给调用链：

- 预取的数据从哪一层到哪一层；通常在哪个阶段完成 L3→L2，何时开始 L2→L1。
- 数据按 token/page/layer 如何组织，何种 layout 支持高效按层搬运。
- 谁分配目标 HBM、谁提交复制、copy stream 与 compute stream 如何交接。
- 找到实际的 layer done counter、event 或 wait 代码：由谁更新，由哪个 attention/KV buffer 访问路径等待。
- 用至少 4 个 layer 的时间线标出 `copy(l)` 与 `compute(l)`；说明能否在全模型 prefix 恢复前开始第一层，哪些前置条件必须满足。
- 区分“按层恢复 prefix”“把长 prompt 切成 chunk”“P/D handoff”，说明能组合什么。
- CUDA Graph capture/replay、动态 page indices、source/destination buffer 复用、取消与释放如何保证安全。没有实现证据的部分明确列出。
- 主线是否支持 L3→L2→L1 的整个过程流水，还是只对其中一段做按层重叠；不要从局部流水推导出全链路流水。

### D. Router 如何利用缓存信息

- 分开解释入口 router / gateway、worker 内 scheduler、cache controller、缓存与存储传输执行者；按真实实现说明执行者是线程、进程还是其他形式。
- router 的前缀视图来自历史请求预测、KV events、外部 indexer，还是其他来源；由配置和版本决定。
- router 的树与 worker 的 radix tree 是否是同一棵，是否拥有物理页索引，能看见 L1/L2/L3 的哪些状态。
- KV 发布、删除、清空与 DP 身份如何传递；router 信息陈旧时谁重新验证命中。
- cache-aware worker 选择如何权衡负载；P/D 如何选择，实际 GPU batch 与缓存预取/复制分别由谁发起。
- 核实“router 根据 radix tree 发布传输任务”是否属于当前主线能力。若主线只选择 worker，而复制由 worker 发起，请用调用链解释。

### E. TP / DP 下的索引与模型附属状态

- 给 TP=8、DP attention=4、CP=1 的具体 rank 与请求例子，区分全局 TP、attention TP、DP、KV shard/replica、逻辑 page ID 和物理页号；若当前配置限制不同，按实际支持修正例子。
- 原生 allocator、radix tree 和请求映射的作用域分别是什么；哪些 ranks 需要保持命中长度、分配或释放的一致性，哪些状态由不同 DP worker 独立管理。
- 解释 page 管理如何对应 token 位置，连续逻辑 token 与物理页布局是什么关系；模型采用 sparse attention 时如何索引需要的 KV。
- 核实 Index K 与 scale 的 L1 保留、HiCache 恢复和 Mooncake 对象完整性；模型不使用它时明确适用边界。
- 区分 Index K、prefix hash、page ID 和 radix node ID。用具体模型说明主 KV 与附属状态如何关联，缓存缺少一种必要状态时如何确定安全复用边界。
- 在原生 P/D 分离路径中，哪些 KV 或附属状态需要交接，交接后 P/D 各自保留什么；区分这些 buffer 的生命周期与本地 prefix cache 的生命周期。

## 交付与讲解方式

先给能让我理解的整体图，再用同一请求示例贯穿树、内存、分层搬运和调度。每个关键结论附本地文件/符号/行号，网上证据附官方链接。

交付一份中文 HTML 讲解，可以设计得有互动感、好看一点，但核心还是把问题讲清楚。页面包含整体架构、radix tree 示例、页映射、请求时序图、layer pipeline 时间线，以及各模块职责表。如果仓库已有文档习惯，沿用它。

互动可以用于逐步播放树的变化、切换缓存命中层级、对照逐层复制与计算；示意时间与实测数据要清楚区分。关键源码依据可折叠展开，正文先讲原理，避免只有文件名列表。

最终让我能说清：一次命中省了哪些计算、仍需搬哪些数据、谁决定和执行、每一阶段等待什么、何时可以复用或回收页。交付前检查 HTML 的图文布局与交互，返回文件路径。
