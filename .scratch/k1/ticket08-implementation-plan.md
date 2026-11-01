# K1-ticket08 实现计划

日期：2026-11-01。本文件保留实施前规划；当前实现和验收状态见[交付说明](delivery-ticket08.md)。
代码核对基点：`fd1541b8d9`，分支 `cryang/dev/mempool-min`。
本轮只写计划和进度，不运行模型、不修改功能、不关闭票据。

## 1. 从这里开始

范围以用户指定的 **syncfile 最新票据**为准：

1. [syncfile ticket08](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/k1/issues/08-k1-05b-prefix-single-copy-numa-interleave.md)。
2. [syncfile 执行文本](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/k1/k1-tickets.md)：第 0、3、12 节；第 15 节用于识别 ticket11 的范围。
3. 本计划；[本地 ticket08](issues/08-k1-05b-prefix-single-copy-numa-interleave.md)只补实施状态和验收历史。
4. [target-alignment 的 D 节](../mempool-min/target-alignment.html#panel-D)、根目录 [AGENTS.md](../../AGENTS.md)、[验收流程](../ascend-mempool/verification.md)。

本轮逐行比对：08 的 What to build、全部验收项，以及执行文本第 12 节与本地一致。
执行文本的本地差别只有 07 关闭的进度补注；票据另有本地状态、依赖链接和 Comments。
syncfile 的 2026-10-31 进度段是历史快照；当前实现状态另按代码和本地验收证据核对。
syncfile 只读，执行前若有新改动，以新稿复核本计划。

核对时的 SHA-256：

```text
syncfile k1-tickets.md
b18a4e3c631faa691328cdbbdcc2a2ebde11bcc8503a4318e36781a5ef98001f
syncfile issues/08-k1-05b-prefix-single-copy-numa-interleave.md
df0aa0118cd406550aa05db8584855e08500fc69d4dd3bb8b25e8ba27f10f5ef
```

执行开始时检查 `git status`。当前已有用户文档改动和未跟踪验收资料；保留它们，提交时显式选择本票文件。
用户本轮要求先规划；后续明确执行本计划时再修改功能。

## 2. 当前进度与本票终点

| 范围 | 当前事实 | 尚未完成 |
|---|---|---|
| sparse 阶段 0 / L0 | 最终 ticket04 已获用户 NPU 验收，Main/SR、保存、恢复、后缀及释放流程可运行 | 旧阶段 0 ticket02 文件仍 open，是历史关闭手续，不阻塞 K1 |
| K1 01 / 02 / 03 | 显式映射、共享适配与记账、split_read 已分别获用户验收 | 03 清理后的版本未另跑 NPU，保留原证据边界 |
| K1 05 / 06 | P/D alive 主 KV 各 8 份，分工写入和释放已获用户验收 | 06 清理后未重跑 NPU；性能和物理驻留不据此宣称通过 |
| K1 07 | P alive Index K 单份已实现；用户今天确认关闭 | 严格九例脚本仍 failed；002 未跑故障注入/重启，NUMA 与性能未完成 |
| K1 08 | 03、05、07 均 closed，前置全部满足 | 本票代码及 NPU 验收均未开始 |
| K1 11 → 09 | 接在 08 后：先换池间搬运，再做最终组合验收 | 均 open；04 微测和 10 旁路诊断也 open，但不阻塞主线 |

07 的具体证据见[用户确认](verification/ticket07-cold-20261101/acceptance.md)：
源快照与正常恢复一致；歧义题的冷、热输入输出相同，均输出 26 tokens。
用户接受这个模型答案行为，不等于严格答案、完整 V1–V10 或所有硬件项目通过。

五项副本数按 **P alive 主 KV / D alive 主 KV / P alive Index K / prefix 主 KV / prefix Index K** 排列：

```text
当前：8 / 8 / 1 / 16 / 16
08后：8 / 8 / 1 /  1 /  1
然后：08 用户验收 → 11 用户验收 → 09 最终验收
```

08 把一份 prefix 分成 8 个 NUMA 段。主 KV 和 Index K 同页、同段；八段合起来各是一份完整数据。
每页只有一个物理目标；16 个进程映射它，不代表 16 份副本。
本票保持 layer-first、B=128、alive 整槽、串行单请求、无淘汰和默认退出。
op1/op3 继续用现有 `unidex_copy`；独立执行器与批量异步接口属于 11。

## 3. 已核对的代码与实现选择

### 3.1 复用的代码

| 现有位置 | 可复用行为 | 本票缺口 |
|---|---|---|
| `sharing_plan.py:PoolSession`、`manager.py` | 07 已把 BM 会话扩为 P/D 共 32 参与者；主 KV 仍按组共享 | prefix 仍归每个 P rank 独占；view 没有段参数 |
| `prefix.py:PrefixStore` | 页状态、源保护、完成 event、索引拷贝、源快照 | 所有页都在一个本地 view；admit 每 rank 全量复制 |
| `split_read.py`、`PrefixStore._submit_load_back` | 两块汇总缓冲、in-place all_gather、一次槽位表、原生 scatter | 只按请求页序号轮转/分块，尚未按物理段选读者 |
| `mempool_subrouter.py` | 有序 tick、全 rank 事实汇总、延迟 ack、原生释放门控 | 取最小空闲页；prefix 描述符固定 `replica=rank` |
| `mempool_router.py` | opaque 描述符、哈希链、保护/发布/回收、容量账本 | `_match_prefix`、`_plan_read`、`_publish_prefix` 写死 16 个 prefix 位置 |
| `mempool_accounting.py` | 主 KV 按物理组覆盖，Index K 单独覆盖，全部读者归零 | 优先复用，测试证明分段 fill 仍满足现有语义 |
| 两轮 session 验收与 CPU fake | 自运行准备、服务、请求、报告、清理；可控 copy/event/collective | 检查器和部分测试仍假定私有 prefix；Index K 开关还会触发九例实验 |

相关历史实现：03=`e6b8f6f487`，05=`cf2069abf5`，06=`9b760a1add`，07=`420270d493`。
以当前净代码为基础；历史提交只用于理解，不重新引入已清理的探针、性能或 shutdown 改造。

### 3.2 配置与逻辑容量

建议新增 `prefix_shared`，默认 false，保持已有配置可运行。
共享验收默认打开 P 主 KV、D 主 KV、P Index K、prefix 四个共享开关。
prefix 共享依赖 P 主 KV 与 P Index K 的共享能力；D 是否共享沿用独立配置。

共享模式的默认值：

- `prefix_pages=40` 表示**单份 prefix 的总容量**，每段 5 页，不是每段 40 页。
- 分组、owner 复用 `prefill_groups`：组首成员为 owner；NUMA 取 P 侧已应用映射。
- `prefix_read_assignment="by_segment"`；保留既有 round_robin / contiguous 作可选对照。
- 起始段策略默认按请求轮转，提供固定起始段的简单配置对照。
- admit 默认 `owners`，另有 `single_rank` 和指定执行 rank（默认 0）。
- Index K 写者沿用 `prefill_index_writers`；默认 rank0，已有双写者配置维持同一所有权规则。

配置字段最终命名可随现有风格调整，但上述行为和默认值不得隐式改变。
段数、owner、组内成员数从拓扑推导；验收拓扑固定为 8 段、每组 2 rank。
不为本票扩展任意异构容量、远端 prefix 或通用配置框架。

保留 Main 的逻辑额度口径：prefix 总页数 40；alive 仍按每份 16 个整槽预留。
物理页数/字节按注册副本数展开：P/D 主 KV 乘 8，P Index K、prefix 两类各乘 1。
不要把 Main 的逻辑额度也乘 8 后再扣一次；V6 分开核对逻辑额度与物理展开量。

### 3.3 页编码与分配

新增一个小型 NPU 纯逻辑模块，建议名 `prefix_layout.py`，集中描述段、页编码、分配规则。
沿用已有 wire 字段，以 `logical_page` 编码 `(segment, offset)`，Main 不解析 NUMA 或地址。
等容量时建议 `logical_page = segment * pages_per_segment + offset`。
描述符保留 `pool/region/replica/logical_page/page_size`；共享 prefix 使用共同 region、`replica=0`。
主 KV 与 Index K 共用这一描述符，分别解析自己的分层 view。

协调者维护每段空闲表；沿用现有 TP 同步状态，使各 rank 得到同一分配结果。
在新请求的首次 alloc 记录起始段，并将游标推进一次；重复 tick/op 不再推进。
admit 第 j 个新页优先段 `(request_start + j) % segments`。
目标段耗尽时选剩余页最多的段，平局按稳定段序选择；段内取最小空闲 offset。
只选页，不改变哈希链和 place(hash)；无新页的请求仍不发起空 admit。

40 页例子：A 从段 0 保存 32 页，每段 4 页；下一请求从段 1 保存剩余 8 页，每段再 1 页。
首轮 A 的目标页序列为 `0,5,10,15,20,25,30,35,1,...`。
原始请求页位置与物理页号分别保存；按段聚合时保持对应关系。

### 3.4 物理布局、映射与探针

复用 07 单会话，不新增第二个 BM 会话，也不另建 POSIX 共享内存后端。
`PoolLayout.prefix_*` 继续表达总的逻辑容量；`PoolSession.regions(participant)` 生成真实物理贡献：

- P 组 owner：本组 alive 主 KV + 本段 prefix 主 KV/Index K；rank0 另有单份 alive Index K。
- P 非 owner：无数据贡献。复用 D 已有做法，将其启动身份探针放到 owner 的分成员保留区。
- D 保持现有主 KV 分组贡献；所有参与者仍正常 join/map。

这是必要的配套修改：当前 `contribution_for()` 仅对 D 的无区域成员返回 0。
若只去掉 prefix payload，P follower 仍会为 64-byte 探针按 GiB 对齐各分配 1 GiB。
同步调整 `probe_gva()`、plan/report 和 placement owner，使 P 的零贡献成员有独立探针槽。

view 增加仅 prefix 所需的段选择，返回该段实际几何及本进程地址。
**每段每层的 stride 是 5 页；不能在原 40 页 stride 上只加一个段起点。**
保留总容量几何用于注册、哈希和额度，拷贝的 bounds/stride 使用段 view 几何。

当前计算侧索引拷贝需要设备地址。所有执行 fill 的 P 分片要读全部 prefix 段；
admit owner 还要访问 NUMA0 的 Index K。沿用现有会话设备映射，覆盖真实访问区域。
执行文本第 0 节及 12.1 已允许按实际访问扩大注册范围；单页物理存储仍仅一份。
BM HOST GVA/`LOCAL_HOST` 不能直接视为 CPU 可解引用地址；前票已在实机证伪这种做法。
本票不添加 ctypes/memcpy CPU 读 BM，也不声称已实现“只有本段设备注册”。

NUMA 在 `create2` 前按 owner 映射传 BM flags；沿用现有 SDK 分配途径。
不在已触碰的 BM 地址上补做 userspace mbind。物理驻留仍由现有采样工具逐段证明。
报告分别记录物理段 owner/bytes、映射者、设备可访问范围和驻留证据。
SDK 无独立注册计时接口时保留 `registration_seconds=null` 和原因，不把 create 时间冒充注册耗时。

按当前 GLM 几何与 GiB 对齐静态推算，零贡献 follower 方案的 P BM 为 **96 GiB**、D 为 **8 GiB**。
这是计划值，最终由 `memory_budget()` 与实际注册重算；不代表 RSS、UB capture 或 NUMA 驻留实测。
验收 CPU 源快照内存另计；即便 prefix 数据单份，16 份诊断快照也不能漏计。

## 4. 三个数据操作与完成语义

### 4.1 admit / op3

协调者一次预留全局目标页，所有分片保留同一逻辑预留状态。
默认由段 k 的 owner 执行本段页：主 KV 从本组 alive 读，Index K 从单份 alive Index K 读。
一个 owner 可能复制本组另一个成员写出的主 KV 页；不能直接取 owner 的原生 HBM 代替 alive 源。
按段、数据类型、层批量提交既有索引拷贝；不改成逐 token 调用。

`single_rank` 模式由指定 rank 从自己的 alive 副本和共享 Index K 写全部段。
其余分片不执行 copy；仍参与已有控制 tick、源保护和完成汇总。

每个分片回报实际写入页集、行数/字节和完成 event，空任务明确为零。
SR 核对每个目标页恰有一个写者、所有非空任务完成后才形成 admit_ack。
复用现有 16-rank transport 回执；“8 owner 完成”不要求发明第二套 8-rank wire 协议。
Main 从全部一致的逻辑页列表发布**每页 1 个位置**，按 parent → child 顺序发布。
延迟任何 owner 时不得 publish、解除 source hold 或归还 alive 额度。

### 4.2 load_back / op2

按描述符的实际段选读者：组 k 负责段 k；组内按配置平分该段命中页。
传入分配函数的是“请求页位置 + 物理页号”，不能只传 `range(hit_pages)` 后按模数猜段。
`width=max(len(rank_pages))` 决定 all_gather 固定宽度；零页 rank 也参与每层集合通信。
H2D 只读取有效页；补齐槽位只参与通信，不读取假页、不生成 scatter 目标。

沿用 03：主 KV/Index K 各一块汇总缓冲，直接写本 rank 段，in-place all_gather 后一次 scatter。
同一槽位表跨所有层和两类数据复用；native HBM indices 仍来自原生分配器，可不连续。
设备 event 完成、全部 rank 恢复完成后 ack；集合通信继续仅由有序 tick 发起。

### 4.3 fill / op1

每个 P 主 KV 组从全部段直接读出命中页，写自己的 alive 副本。
组内分工继续使用现有 `PageWriter`，按**完整 prompt** 的逻辑位置分配，与后缀写回一致。
分段只改变源地址；目的位置仍是请求里的原始 token 位置。
Index K 默认仅 rank0 从所有段填回单份区域；可选双写者沿用 07 的页分工。

按 `(source segment, region)` 组织拷贝，由分片解析每层地址；尽量跳过空掩码提交。
保留独立 read stream、完成 event、各组覆盖位图和 Index K 覆盖门控。
正常路径仍允许原生 HBM 释放后 fill；数据源必须是 prefix。
任何组或 Index K 写者未完成时，`ready_tokens` 不能达到完整 prompt。

### 4.4 字节口径

令命中 n 页，主 KV 每页 M bytes、Index K 每页 I bytes。

| 操作 | 实际数据总量 | 含义 |
|---|---|---|
| load_back | `n * (M + I)` 从 prefix 读出 | 每物理页只读一次；all_gather 通信量另列 |
| fill 主 KV | `8 * n * M` | 每个主 KV 组读完整命中一次，组内不重读同页 |
| fill Index K | `n * I` | 目标只有一个 Index K 区域 |
| admit | `new_pages * (M + I)` | 每新页只写一次 |

“每组读八分之一”指八段均匀时，每段约占该组完整命中的八分之一。
每组仍需读齐全部段；不能误实现为各组只拿完整命中的八分之一。
31 页命中或段耗尽回退会有实际读量差异；补齐通信宽度相同不等于 BM 读字节相同。
默认 admit 的**主 KV**源与目的同 NUMA；Index K 源在 NUMA0，其他段自然有跨 NUMA 读取。
单 rank admit 也不是全部本地；报告按配置区分，不能统一声称所有拷贝 100% 本地。

## 5. 验证策略与不可混淆的证据

### 5.1 内容 oracle 必须跟随真实源写者

当前 `_saved` 是每 rank 的计算快照，`_verify_read` 用本 rank 的快照比较 native HBM。
单份 prefix 后，同一页只从某个组的 alive 保存；Index K 又来自独立写者。
不能把接收 rank 自己未参与保存的快照自动当作期望，更不能拿目标数据生成期望后自比。

验收模式下复用计算源快照，按 main/index 分别记录每页摘要：

1. 主 KV 期望来自 admit 源组内实际负责该请求页的 `PageWriter`；Index K 来自对应 Index K 写者。
2. 在已有有序 SR 汇总中传递小型页摘要，关联到已预留的全局 prefix 页。
3. 正常 load_back 的 native HBM 分别与这两类源摘要比较；无需额外 BM 读取。
4. 已通过上述独立比较的恢复数据可作为后续请求已命中部分的参考；后缀仍用实际计算源。
5. 私有模式保留原有校验；共享模式的离线 checker 同样按真实来源关联，不按接收 rank 索引位置。

实现以少量现有 receipt/report 扩展完成，不另建通用校验流水线。
CPU 测试让不同源组/写者拥有可区分的值，证明读取的是规定来源，不靠所有 fake 都填相同常量通过。

### 5.2 用户约定优先于同步稿中的旧验收表述

- 性能代码、perf 配置和运行工具留 `mempool-min-baselinetest`；当前分支只做功能和所需正确性证据。
- 不新增直接读取 BM 的验收；用户在 03/07 已明确此约束。已有启动身份探针/主 KV 探针沿用。
- CPU 能检查全部分段数据与 8 份 fill 目标；NPU 只用源快照、正常恢复内容、真实拷贝任务和完成事实。
- op1 后 8 份 alive DRAM 的直接内容比较、页驻留及性能分别列为未验证，不能用本机通过替代。
- 严格报告和人工接受结论分别保存；08 仍须用户确认自己的硬件验收，不能继承 07 的特例自动关闭。

### 5.3 精简 CPU 覆盖

复用现有 `MappedCopy`、`GroupSDK`、`ManualEvent`、`TensorCollective` 和真实 Main/SR 测试接口。
不启动模型；增加或扩展以下行为测试即可：

| 场景 | 必须观察到的结果 |
|---|---|
| 两请求交错、重复 tick、耗尽一段 | 起始段按请求轮转；分配幂等；最大空闲段回退；无重复物理页 |
| 32 参与者映射与贡献 | 每段只有一个 owner，两类总共 40 页；P follower=0；所有 peer probe 互不覆盖 |
| 多层跨段 admit | 每目标页单写者；源组与 Index K 来源正确；最后 owner 延迟阻止发布/释放 |
| 分段 load_back，1/31/32 页 | 所有 rank 完整恢复；仅有效页被读一次；空读者仍进入 all_gather；原生非连续页正确 |
| fill 主 KV 8 份 + Index K 1 份 | 每目标页写齐且只写一次；完整 prompt 的 writer 分工不被 hit_len 改写 |
| 真正的 Main/SR A/B/C/E | 目录每页 1 个位置；占用 32→40→40→40；hit=0/4096/3968/0；原有保护与 free_ack 顺序 |
| 错误证据 | 错段 stride、双写/漏写、corrupt native 页、缺最后完成事件、错误副本数均被对应接口/检查器发现 |
| 旧配置 | prefix_shared=false 的私有布局、原两种 split_read 和既有入口保持可运行 |

定向红绿后集中运行一次完整 CPU 套件；若有失败，修复后只复测受影响集合。
同一测试每轮最多两次；额外覆盖裁减在报告中说明，不重复初始化模型/服务。
沿用本仓库现有静态要求：ruff/格式、isort、Python3.9 AST、修改功能模块 strict mypy。
最近可用解释器为 `/private/tmp/k1-ticket05-fix-venv/bin/python`，执行前确认路径仍存在。

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/k1-ticket05-fix-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

这条命令在本规划轮没有执行。socket/进程类测试如受沙箱阻断，按权限流程只补对应项目。

### 5.4 NPU 入口

建议新模式 `prefix-sharing-acceptance`，复用两轮 target/restart 编排。
每机一条命令；同提交、同配置、同 run-id，日志放 `/home/cryang/log/mempool-min/<run-id>/`。
入口自动准备、等就绪、发送请求、检查结果、收报告、清理自己启动的进程；失败非零退出。

每轮复用 A/B/C/E + 长生成/EOS 六请求；首轮默认多 owner admit，第二轮可用 single_rank 对照并验证同端口重启。
若第二轮切执行者配置，明确记录 effective-config 差异；首轮故障注入、第二轮正常退出流程复用。
起始段、段不足回退和不均匀页数先以 CPU 充分验证，不另启多套 NPU 服务。

**不要因为 `prefill_index_shared=true` 自动附加 07 的三道逻辑实验。**
当前 runner 和 summary 有此耦合；以显式验收模式选择用例，保留旧 07 九例和失败报告。
本票用现有六例的独立答案；B/C 同时有内容、命中长度与释放顺序证据。

结果新增：单份分段注册、applied 拓扑、分配日志、实际写者/页集合、分区域字节、逐段驻留状态。
V1–V10 逐项标实际覆盖；V3 的逻辑答案、V8 的注册 CPU 重放维持既有边界。
默认六例精简入口不能标成原始完整 V1–V10 全通过。未完成项目交付时列出，由用户核对验收范围。
用户运行 NPU 后才能记录结果；本票关闭后才允许 11 开工。

## 6. 文件阅读与预期改动顺序

所有预期功能改动都位于 Ascend/NPU 专用目录；通用主线功能源码预期 **0**。
若实施时发现必须修改这些目录外的功能源码，先单独说明具体文件、必要性及 hook 不足，再动实现。

路径简写：`NPU=python/sglang/srt/hardware_backend/npu/mempool/`；
`SR=python/sglang/srt/disaggregation/ascend/`。

| 顺序 | 功能源码 | 用途 |
|---|---|---|
| 1 | `SR/mempool_router_protocol.py` | 配置、默认策略、注册副本数；现有 wire 消息类型不变 |
| 2 | `NPU/prefix_layout.py`（建议新增） | 段几何、逻辑页编码、确定性分配；纯逻辑、无设备地址 |
| 3 | `NPU/sharing_plan.py` | 物理段贡献、owner/成员、会话签名、计划/applied 报告 |
| 4 | `NPU/manager.py` | 按段 view、本进程地址、零贡献 P follower、分成员探针及注册证据 |
| 5 | `NPU/split_read.py` | 按真实段分配读者，复用组内页划分 |
| 6 | `NPU/prefix.py` | 按段 admit/fill/load_back；完成事实、源保护及内容证据 |
| 7 | `NPU/runtime.py` | 将共享布局和策略接入 PrefixStore；保持模型写回行为 |
| 8 | `SR/mempool_subrouter.py` | 选页、写者任务汇总、共同描述符、单写者覆盖与完成门控 |
| 9 | `SR/mempool_router_transport.py` | 配置、分片、注册与物理布局接线 |
| 10 | `SR/mempool_router.py` | 单位置发布与读取；按注册位置数量处理，继续不持有物理地址 |

按实际必要性才修改 `NPU/layout.py`/`config.py` 的几何承载或 `SR/mempool_accounting.py`。
`mempool_prefill_restore.py`、`restore_kernel.py`、模型 forward、D 写回和通用 scheduler 预计复用。
这是一份预期文件图；交付时按最终净 diff 重新生成源码清单。

脚本、测试、说明文档单列：

- `ascend-mempool-test/scripts/run_mempool_min.py`：新模式、准确预算、两轮执行。
- `verify_mempool_session.py`、`verify_mempool_shared.py`、`verify_mempool_prefix.py`、`verify_mempool_reuse.py`：解除16份假设并消费真实分段证据。
- 现有 NUMA planner CLI/检查器：按新增区域更新输出；复用 placement 工具。
- `tests/unit/test_pool_session.py`、`test_prefix.py`、`test_prefix_split_read.py`、`test_mempool_router.py`、`test_mempool_subrouter.py` 等：在原有接口上扩展行为测试。
- 可新增一个 `test_prefix_shared.py` 收纳组合数据行为，避免多份重复夹具。
- `ascend-mempool-test/K1_TICKET08.md`：最终实际可运行的逐机命令、结果文件与覆盖边界。
- 本地 ticket08 Comments、交付说明、验证记录、target-alignment D 节：同步实际进度。

## 7. xhigh 执行步骤与完成条件

每步完成即更新 D 节并重新生成 HTML，记录“代码已实现 / CPU 实际结果 / NPU 未执行”。

1. **配置与物理分段。** 先补小型池接口红测试，再实现页编码、会话贡献和段 view。
   完成条件：CPU 证明 8/8/1/1/1，P follower 零贡献、探针唯一、跨层 stride 正确；旧模式仍通过。
2. **冷请求保存贯通。** 接入交错选页、owner/single_rank admit、SR 汇总及 Main 单位置发布。
   完成条件：A/B 增量保存与满池行为可用；无重复写；延迟最后 owner 时不能发布或释放。
3. **命中恢复贯通。** 接入按段 split_read、fill 的 8 份主 KV 与单份 Index K，保持现有覆盖门控。
   完成条件：CPU 跨段 1/31/32 页、非连续 HBM、两种 admit 执行者及 A/B/C/E 全链路通过。
4. **验收证据与入口。** 参考来源按真实写者关联；更新注册、容量和内容 checker；完成两轮自运行入口。
   完成条件：两侧 `--describe` 能准确说明配置/预算/模式；假证据会失败；无新增请求级 BM 读回或 perf。
5. **集中验证与交付。** 跑本机必要检查，按 `code-review` 做 Standards/Spec 双轴评审并处理发现。
   完成条件：单票实现/测试/说明尽量合为一个 commit；交付最终源码阅读表和 NPU 命令；08 保持 open。

计划执行到第 5 步即进入用户 NPU 验收阶段，不自动开展 11、不自动关闭 08。
不在本票提前实现线程执行器、批量异步传输、并发请求、eviction、page-first 或通用 shutdown。

## 8. 本轮验证记录

- 已只读核对 syncfile 08/09/11 与执行文本，并记录范围与本地差异。
- 已核对当前 HEAD、未提交工作区、六张 K1 已关闭票及 07 的接受边界。
- 已读现有布局、manager、PrefixStore、Main/SR、记账、恢复和验收入口的实现。
- 已静态计算分段与零贡献 follower 的预算；不是运行硬件或功能测试。
- 文档检查通过：16 个本地链接有效；两份 HTML 的内嵌源稿与 Markdown 一致；STE 均零警告。
- 本轮涉及的已跟踪文档 `git diff --check` 通过；新 Markdown 无尾空格或未闭合代码围栏。
- 本轮交付为本计划、HTML 总览和本地进度链接；08 代码、CPU 新测试和 NPU 验收均未开始。
