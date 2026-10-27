基于刚才对 SGLang 主线 GPU prefix cache、radix tree、HiCache、Mooncake、HiSparse、layer pipeline 和 router / scheduler 的讲解，现在请阅读下面的 mempool 设计背景，先和我讨论理解不清、存在矛盾或尚未决定的地方，逐步收敛一份初步设计。继续用中文，以实际代码为证据。

下面是目标架构，尚不代表 SGLang 已有能力。请清楚区分“已有实现”“已确认设计方向”“候选方案”和“未决项”。

## 先讨论，再整理

当前阶段采用对话和简短 Markdown 记录，暂不生成或修改 HTML，也不开始代码实现。等我明确提出整理 HTML 时，再结合讨论结果更新页面。

1. 第一轮用一小段话复述你理解的数据归属、复制路径和职责分工，再挑最影响架构的 2–3 个问题开始讨论。
2. 每个问题用具体请求或容量场景说明为什么要决定，给出可行选项、主要代价和你的初步建议。先说明问题，再让我选择；问题清楚时不必凑齐多个选项。
3. 优先沿用此前框架讲解的证据。需要补查源码时，只追踪足以回答当前问题的调用链；及时开始讨论，不必先完成下面所有分析项目。
4. 聚焦 demo 的主流程、数据完整性和资源生命周期。实现细节按讨论需要展开，复杂故障协议和后续性能优化先记入待办。
5. 以已确认设计作为讨论起点。若发现矛盾，明确指出具体场景和证据，再提出调整；你的推荐仍是候选，直到我确认。
6. 收到回答后简短更新“已确认 / 仍待讨论 / 后续优化”，再推进下一组问题。最后先形成可核对的 Markdown 草案，保留未决项及其影响。

## 我的 mempool 架构

1. 采用 Prefill / Decode 分离，router 为请求选择 P 实例、D 实例及存储位置。
2. 数据分两类：
   - alive mem pool：保存请求存活期间的 prefill KV 和 decode 新 KV，为 decode 的 sparse token 访问提供存储。
   - prefix mem pool：保存跨请求可复用的前缀，按 page 访问。
   alive 算一个逻辑 pool，prefix 算另一个逻辑 pool；alive 内可以有分布在不同 NUMA 的相同数据 replica。
   两类之间不做物理去重。同一前缀可以有 alive 与 prefix 两份副本。
3. 一致性哈希只用于 prefix mem pool 的节点放置。alive 默认按 KV 生产实例的本地 NUMA 拓扑配置，使用可用本地 NUMA，不为 DP attention 增加额外负载均衡。P 生成的数据默认留 P，D 生成的数据默认留 D；也可在请求进入时将 prompt 放置到 D 附近，以优化后续 sparse 读取。
4. 两类 pool 都按 page 分配和回收；alive 的读取选择可以细到 token。区分管理粒度、实际复制粒度和计算粒度。
5. prefix 节点内有独立传输进程，router 提交 source、destination、页范围和依赖，由该进程执行复制并报告完成。
6. 考虑由 router 管理一个统一的可共享 KV 目录：包含 prefix，以及 alive 中已经完成的 prefill KV；不把活跃 decode 新产生的 KV 发布成可共享前缀。统一目录不预设单个物理 allocation。
7. B 复用 A 的 alive prefill KV 时，demo 将数据复制为 B 自己拥有的副本。已有复制期间保护 A 的源页；B 的后续 decode 使用 B 的存储。
8. KV group 是请求下发后形成的 P/D attention 分组与存储位置之间的逻辑关联；一个 D 分组可接收不同 P 的请求。运行时应能访问全部候选位置，请求绑定只选择其中实际需要的位置。
   - 同一 attention 分组的同内容 replica 保持相同 page 索引和 layout，保留逻辑身份到位置的映射。
   - attention TP=2、DP=4 时，一个 attention 分组的两个 rank 可共享一个 NUMA 副本；attention TP=4、DP=2 时，该组四个 rank 使用两个 NUMA 副本，各供两个 reader。两例在 CP=1 时均对应总 TP=8。
   - 不同 DP 请求、P/D 不同内容段无需统一本地物理页号；同内容 replica 的 base address 也可以不同。
   - demo 支持 P/D attention TP 宽度不同，但 prompt 副本先沿用 P 侧布局。按 D 的 reader 拓扑扩展副本留待后续优化；需要说明由此产生的读取集中问题。
9. 已确认将 prefill 的 Index K 与必要 scale 作为 alive 附属数据保存在 DRAM，供复用和 backup。P 释放该请求 HBM 后，复用方仍可恢复完整状态。D 的计算继续使用所需设备侧 Index K；DRAM 附属副本的布局、副本数及写入时机仍需细化。Index K 是模型数据，不是 prefix hash 或 page 索引。
10. router 根据可用容量选择 P/D 与存储位置并预留资源，管理可共享目录和传输任务；具体页由相关 TP ranks / 存储 owner 协调分配。当前不要求按命中 prefix 源的速度选择计算实例。
11. alive 中 P/D 数据保持分散，sparse 访问通过源位置与偏移取数。保存到 prefix 时，接受异步字节复制直接写入目标页的对应偏移，让目标在复制时成形；以不明显损害 backup 性能为条件，不要求先整理 alive。

## 一次请求的目标流程

这里 P_B / D_B 表示请求 B 选择的 prefill / decode 实例，B 本身不是机器名。

1. router 查找可复用 prefix，按容量选择 P/D 与存储位置并预留，相关 owner 协调分配具体页，保护缓存源。源可以是 prefix mem pool，也可以是另一请求可复用的 alive prefill。
2. prefix 命中时，优先把命中数据恢复到 P 的 HBM，尽快启动未命中部分的 prefill。
3. P 生成新 KV 的同时，缓存传输进程把命中段复制到 P 的 alive DRAM。P 只向 DRAM 写新产生的 KV。命中段和新算段的目标范围不重叠。
4. P 的 alive DRAM 主要供该请求后续 decode 做 sparse KV 访问。恢复到 P HBM 和填充 P DRAM 是两个不同目标。prefill，包括 chunked prefill，消费本实例 HBM 中需要的历史 KV。
5. 缓存源→P HBM 的恢复可能参考 SGLang 的 layer pipeline：如果某层需要的 prefix 已就绪，就计算该层，后续层继续复制。这是候选方案，需要结合刚才讲解的主线行为判断如何实现。
6. 传输 Index K 和所需 handoff 状态。D 依赖的 prompt DRAM、Index K 和 handoff 全部满足就绪条件后才开始 decode；具体无依赖部分可以重叠。D 新产生的 KV 写入自己的 alive 存储。
7. decode 完成后，安全回收该请求的 alive 数据；根据准入策略选择是否保存到 prefix，向 router 报告新物化 KV 的 hash 与可用位置。区分生成完成、异步操作完成和物理页回收。

上述 P DRAM 描述默认放置。若入场时选择 D 侧保存 prompt，命中段复制和 P 新算段写入均指向该目标；P HBM 仍用于 prefill。

## 优先讨论的议题

以下均为未决项，先从前三项挑选问题；按我们的回答调整顺序，不必一次全部询问。

1. **一致性哈希的放置单位。** 每个 page 独立放置，还是连续若干 page 作为一个放置单位？page 管理粒度不必等于放置粒度。结合长前缀恢复和分散 P/D 数据的 backup，讨论容量均衡、大块复制和任务数量的取舍。
2. **可复用数据何时发布、哪些值得保留。** alive prefill 是整段完成后发布，还是逐 chunk 发布？必要 KV、Index K 和 scale 应满足什么完成条件？请求结束后，prefix 的准入如何选择 prompt 与已物化的生成部分，空间紧张时优先保留什么？这里不改变活跃 decode 新 KV 不进入共享目录的约定。
3. **容量不足时的基本行为。** alive 空间不足时选择其他实例还是等待？prefix 满时淘汰、跳过缓存或采用其他策略？router 如何按实际 replica、附属数据和已预留容量理解可接纳请求量？先明确基本行为，再按需讨论请求进入时预留多少 decode 增长空间。
4. **恢复与 backup 的 demo 起点。** 优先恢复 P HBM、同时填充 alive 的目标已经确定；需要讨论首版采用整段恢复还是按层流水，以及后台复制如何满足 decode 就绪条件。结合 page_size=16、prompt 长 30 的例子，判断 D 段对齐和目标 prefix 布局是否需要现在决定。
5. **Index K 附属存储的最小合同。** 存入 alive DRAM 已确认；进一步讨论它如何关联主 KV 页、何时完成备份，以及 A 释放 P HBM 后 B 复用时需要保护哪些源。副本数与容量计算仅展开到能判断方案是否可行的程度。

也可以提出你从源码或具体场景发现的其他问题，但优先解释它会影响哪一步请求流程。

## 按讨论需要查证的参考问题

以下清单帮助核对主线机制与目标设计，不是第一轮必须完整交付的分析报告。

### A. 与主线机制的对应关系

- 讨论涉及某个模块时，按“现有机制 / 目标需求 / 差异 / 建议 / 未决项”说明对应关系，覆盖树、allocator、请求映射、分层复制、storage、router 和生命周期。
- 区分 alive/prefix 的生命周期分类与 L1/L2/L3 的存储层级，说明哪些能力相近、哪些职责不同。
- 解释 page 管理与 token sparse 访问如何共存，连续逻辑 token 不一定物理连续时如何组织复制。
- 比较原生 L1 共享引用与这里选择的复制复用：内存、带宽、源页保护、独立回收分别有什么代价。

### B. HBM 优先恢复与两条 DRAM 写入路径

- 用同一请求 B 画出缓存源→P_B HBM、缓存源→P_B alive DRAM、P_B 新算 KV→alive DRAM 三条路径。
- 标出命中段与未命中段、按层和按 page 的完成条件，说明 prefill 可以何时开始，D 必须等待什么。
- 将主线的 copy/compute streams、逐层事件、buffer 保活、CUDA Graph 和 page 映射机制逐项映射到该流程。
- 对比整段恢复与按层流水。哪些重叠已有主线证据，哪些需要新增协调；L3 预取是否在计算开始前完成，按实际路径讨论。
- 分析 HBM 优先恢复与后台 DRAM 复制竞争源端带宽时的任务优先级和背压，保持数据就绪与源页生命周期正确。
- 单独讨论 chunked prefill、prefix 部分命中、尾页和部分复制完成，给出可核对的候选策略。
- 用 page_size=16、prompt 长 30 的例子，分析分散 P/D 数据如何直接写入 prefix 目标页；比较 D 段从本地起点或请求绝对位置对齐，说明分段复制、连续合并和容量的取舍。不要把异步直拷等同于没有提交开销或任意 layout 都可一次复制。

### C. KV group、TP / DP 与索引一致性

- 同时用总 TP=8、DP attention=4 和 DP attention=2、CP=1 的例子，说明每组一份或两份 replica，以及不同 DP 请求的相同 local page 数字为什么可能表示不同数据。
- 区分可访问同一目录的集合、共享同一逻辑 KV 的 replica 集合，以及必须协调 allocator 的 rank 集合。
- 讨论 KV group 覆盖多个 attention TP 组或实例时，需要统一哪些身份和索引，哪些可以用映射解决。
- 在已选择的同内容 replica 页索引一致约束下，分析 owner 如何协调分配与回收。区分统一逻辑身份、相同 allocator 页号、相同元素 layout 和不同 base address。
- P/D 并行布局不同时，按“prompt 沿用 P 副本布局”的 demo 约定分析正确性与性能，再提出后续优化；区分 MLA replica 与 GQA head shard，说明 page 描述符和恢复布局需要什么信息。

### D. 完整缓存状态、目录与放置

- 核实相关模型的 Index K 与 scale 如何随 KV 保留、迁移和恢复，沿用原生的可恢复边界思想。
- 按已确认的 alive DRAM 附属存储方案，说明 A 的 prefill HBM 释放后，B 如何恢复 Index K / scale，谁保护源页；细化副本数、容量计费、备份调度与发布就绪条件。
- 先区分原生 P/D 交接后的请求引用释放与真正的 HBM 缓存淘汰，再分析 L1 保留、HiCache 的 INDEXER host sidecar、共享 L3 和无完整缓存时的行为。若对照 HiSparse 的活跃 host KV，核实其 radix cache 兼容限制，不能假设它已经提供跨请求复用。
- 统一目录应记录哪些内容身份、布局、位置、generation、就绪状态和附属数据；同内容在 alive/prefix 的多个位置如何表示。
- 一致性哈希如何影响 prefix 的容量分布、热点、迁移与长前缀分散访问；结合逐页散放对 backup 连续复制的影响，分析放置单位。alive 默认采用生产实例本地放置，入场时靠近 D 的放置作为候选优化。
- 用主线的实际调用链划清 router、worker scheduler、allocator 和传输进程的职责。router 不直接管理空闲物理页；分析 owner 如何上报扣除 replica 成本、已有预留和未完成回收后的逻辑容量，如何避免重叠请求 KV group 重复记账。

### E. 发布、完成与回收

- alive prefill 何时可以对外发布，逐 chunk 发布需要哪些完成条件。
- 请求结束后 cache admission、复制、hash 发布与 alive 回收如何排序；hash 对应已物化 KV，而非仅已采样输出。
- 区分“停止接受新的复用”“已有复制 drain”“请求引用释放”“物理页回收”。
- 用 A 正在结束、B 正在复制 A 前缀的例子，检验保护源页、重试、取消和迟到消息的处理。
- prefix 复制失败、目标空间不足、源失效或目录过期时，区分缓存复用可回退与当前请求正确性所依赖的状态。

## 证据与讨论记录

沿用刚才框架讲解中的源码证据，针对差异补充实际调用链、文件/符号/行号。若 checkout 或配置变化，先复核对应结论；网上补充材料使用官方链接并注明版本。

当前输出以简短解释、具体例子、必要的小表格或文本时序为主。每轮记录我的选择及其理由，保留候选方案与未知事实的区别；涉及性能时区分推测与实测。

讨论收敛后，用一个完整请求串起初步设计，整理已确认决策、仍未确定的策略及后续验证项。第一轮的完成标准是让我理解并能回答最关键的 2–3 个问题。
