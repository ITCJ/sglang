---
title: K2 ticket13 实施方案
subtitle: 默认关闭的观测模式；行为、数值和资源生命周期沿用 K1
template: doc
theme: blueprint
lang: zh
date: 2026-11-03
status: 规划完成；功能未实现；NPU 未运行
baseline: dc31b37cbe 与当前未提交资料
---

本轮只规划 ticket13。切换至 solmax 后，从本文 G 节开始实施。
新增文件名和命令均为拟定接口，当前不可运行。

执行依据依次为用户约定、[AGENTS.md](../../AGENTS.md)、
[K2 spec](spec.md)、[ticket13](issues/13-k2-m1-observability-mode.md)、
[执行文本第 5 节](k2-tickets.md)。统计定义承接[交接答复 A/B](handoff-check-20261103.md)。
本文补足源码接入点、时钟限制、图重放验证及交付顺序。

`ask-matt` 路由结论：规格与拆票已有，进入实施前设计。
本轮不重新 triage，不调用 implement，不改功能代码。
同目录切换模型可继续当前会话，无需再建一套 handoff 文件。

## A 代码实现规矩

| 规矩 | ticket13 的具体执行方式 |
|---|---|
| 可运行 demo，保持简单 | 一个观测配置、两份采集模块、一个离线分析入口；不用通用遥测框架 |
| 严格票范围 | 只测量、核查残留、统一证据输出；缓存策略、64 位寻址、页分配留后续票 |
| 优先复用 | 复用 Main/SR、runtime 完成事件、pool-copy 执行器与票09验收编排 |
| 新增防御性代码先说明 | 沿用现有 fail-stop；不顺手增加重试、降级、补偿、看门狗或兼容分支 |
| 专用目录优先 | 采集与归并逻辑放 ascend/npu；通用主线只提供必要的结果完成通知 |
| 生命周期准确 | submit、设备/库完成、tick 观察、ack、publish、source_release、credit_return 分开记录 |
| 集合通信顺序 | 保持现有调度 tick 与图内依赖；网络线程只收发或入队 |
| 默认退出方式 | 父进程清理 worker；不增加逐 rank 退出握手 |
| 精简测试 | 一个行为一个关键 oracle；同一测试最多两轮，合并环境初始化 |
| 实机由用户运行 | 双机各一条自运行命令；日志进 `/home/cryang/log/`；失败非零退出 |
| 单票交付 | 一个 ticket commit；Standards/Spec 两轴评审；文件清单与测试/文档分列 |
| 及时记录进度 | 每步更新目标页 D 并重渲染；CPU 通过、代码完成、用户 NPU 确认分别记录 |

关闭条件继续使用[阶段验收规则](../ascend-mempool/verification.md)。
13 保持 open，直到用户确认本票 NPU 验收。
18、21、22 的依赖不因规划或 CPU 通过而解锁。

## B 已核对的代码事实与取舍

| 事实 | 实施结论 |
|---|---|
| `dc31b37cbe` 已删 P/D 临时探针、人工延迟及搬运计时 | 引用[K1 删除清单](../k1/cleanup-test-hooks.md)，只登记本票实际残留 |
| `MempoolPDService.advance/prepare_batch` 已有 tick 与真实 forward 接入点 | 在已有调用中建立 tick_id、batch_id、request-step 关联 |
| runtime 的 `end_forward/poll_completed` 已支持 eager/replay 的完成快照 | 扩展观测快照；不重新建立完成协议 |
| runtime 完成点早于 scheduler 的采样结果处理 | 用结果完成通知结束 T_step；不把 forward 提交结束当作 token 就绪 |
| miss 路径已有有效选择、slot lookup、当前 token 直供与 P/D 两源 mask | 从实际执行的 mask 取计数；离线按逻辑页聚合 |
| `get_sparsity_driven_kv_offload_sparse_context_len` 返回模型 top-k | 本地尚无独立 B=k/2k/4k 配置；13 不冒充用户并行分支已接入 |
| materialize 每步清空 slot map，再填当前选择 | 本地策略记为 `replace_selected`；不能标成已实现 LRU |
| 通用 IndexerTopkCapturer 当前为 CUDA 接线且要求 attn TP=1 | 不扩展它来承接 NPU TP16；在现有 NPU materialize 处采集 |
| 票09支持 `(page_size_tokens,S_D)=(128,128)/(128,512)/(64,128)/(64,512)` | 保留四组回归；分析主配置用页128，512输出使用 S_D=512 |

历史提交 `7a8a0b955b` 中的 PerfTrace 可复用事件命名和单调时钟思路。
可参考其 Main/SR/Ascend sender 接入位置，不整票 cherry-pick。
旧 first_token_sent 基于输出发送；非流式下不能代表逐 token 就绪。
旧 perf 路径还改过 sampling stop 配置，这部分不复用。

`verify_prefix` 下的源快照和正常 load_back 后设备内容比较仍是正确性依据。
将它们的结构化输出接入统一文件 writer，保持校验开关与观测开关独立。
`observe=false, verify_prefix=true` 仍能完成原功能验收。
协议依赖的写入计数、range 校验和 drain 证据继续工作。

## C 最小数据合同

拟新增配置 `observe=false`、`observe_sample_every=1`。
采样配置只影响页集合/设备细项，不抽样生命周期与步数。
run manifest 记录配置、代码/SDK/kernel/torch_npu/CANN 版本和采样范围。
路径由验收 launcher 设置，沿用现有证据根目录。

每个进程独立写文件，例如 `observe/decode-rank03.jsonl`。
Main 单独一个文件；不把逐页 JSON 混进多进程 stdout。
每条记录包含 `schema_version, run_id, event, host, pid, role, rank, clock_domain`。
关联键使用已有 `rid, room, op_id`，并增加 `tick_id, batch_id, forward_id, layer_id`。
这些是观测键，不增加 ticket15 的控制 epoch 或新的状态机。

| 记录 | 必需内容 |
|---|---|
| run | 页大小、实际缓存容量、top-k、策略名、模型几何、capture/replay、拓扑与样本数 |
| step | 请求列表、tick 入口、forward 提交/完成观察、token 结果完成、执行方式 |
| layer_pages | 每页有效选择、hit、cache miss、IO miss、current_device_supply 与按源字节 |
| selection_share | producer_layer、有效 token 序列逐元素相等结果、有效长度 |
| op | kind、shard、源/目标区域、物理拷贝块与字节、各事件时间、完成证据类型 |
| request_end | prefill/decode forward 数、prebuilt 数、输出 token 数、丢弃输出的 forward、停止原因与末位置 |
| evidence | 保留的 prefix 内容比较与功能计数；由原验收器消费 |

页记录带 `machine, attn_group, reader_numa, sharing_group`。
当前 attention group 是 TP16 的组0；八个 NUMA sharing group 另记。
两种组不能混用。NUMA 字段来自配置/注册，不宣称物理驻留已验证。

页身份分两层：

- 内容：prompt 使用 Main 已有的链式 page hash；生成段使用请求与逻辑页身份。
- 位置：保留 source_host、region、owner/replica、physical_page 和 logical_page。

fork 仅比较共同且不可变的 prompt 内容页。
不同请求的逻辑 page0 不自动视为同一内容，复用物理槽也不沿用旧内容身份。
K1 整槽地址按现有 layout 推导；18 再替换地址解析，日志含义保持不变。

同一页可部分 hit，也可包含多个源。
记录 `selected_count, hit_count, miss_count, io_miss_count`，按源展开 IO 字节。
hit 标签表示所选 token 全命中；miss 标签表示至少一个 token 缺失。
保留 `has_hit` 表达部分命中。

```text
selected_count = hit_count + miss_count
miss_count = io_miss_count + current_device_supply_count
token_cache_miss_rate = Σ miss_count / Σ selected_count
io_miss_rate = Σ io_miss_count / Σ selected_count
page_miss_rate = 有 cache miss 的选中页数 / 选中页数
```

第二条等式限当前供给路径；日后新增供给方式必须增加对应计数。
padding、无效 top-k、图补齐行和通信 padding 不进入有效读字节。
页集合去重，token 计数遵循实际选择；物理字节按真实传输计划计算。
不把一个 miss token 算成整页读取。

producer 比较有效 token 序列和 mask，不能只比页号。
记录名明确为 `ordered_selection_equal`；false 不自动解释为无序集合不同。
共享层保留自身 hit/miss，只有 indexer 计算归 producer。

离线入口拟为 `analyze_k2_observation.py`，提供四个可独立运行的子命令：

| 子命令 | 输出与分组 |
|---|---|
| miss-rate | 逐层按 hot_cache_tokens/策略比较；接受用户三档日志，不拿本地日志模拟不同 B |
| concentration | 按内容页累计 IO miss 行数/字节，输出前 x% 页占比，两种单位分开 |
| cross-step | 同请求同层，`交集页数 / 当前步页数`；只比较真实相邻步，不把采样间隔当一步 |
| fork-overlap | 串行兄弟请求、共同 prefix、相同 decode 步与层，计算页集合 Jaccard |

另提供 summarize，一次调用四项并核账在线计数。
先出 JSON/CSV 与 Markdown 汇总表，不加绘图依赖或交互仪表盘。
空分母输出 null；不完整窗口标出覆盖范围。
真实三档日志未到时，该验收项保持 pending，CPU fixture 只验证算法。

## D 图内采集与时间分解

**先验证图重放的计时依据，再铺开逐层打点。**
首选复用现有 `torch_npu.profiler` 的 CPU/NPU 时间线和图任务关联。
范围包括实际 replay 的算子、拷贝、HCCL 与 stream wait。
模块级标记用于建立 layer/task 对应，不将 Python hook 用时作为设备用时。

普通 NPU Event 不能直接套 CUDA 的 `external=True` 用法。
官方当前源码中 Event 与 ExternalEvent 的能力不同；ExternalEvent 未启用计时。
`elapsed_time/recorded_time` 的实现会调用同步，因此不逐层读取。

参考 [Python Event 定义](https://github.com/Ascend/pytorch/blob/master/torch_npu/npu/streams.py)与
[NPUEvent 实现](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/core/npu/NPUEvent.cpp)。
这些是 2026-11-03 查询的上游接口事实，不能证明部署版本的能力。

第一步交付一个无模型 Graph 检查入口，由用户运行。
同一张图以两组输入 replay，验证以下事实：

1. 稳定观测 Tensor 随 replay 更新，且两步的记录不会互相覆盖。
2. 主流、miss 流与 wait 的设备时间可定位到不同 replay。
3. capture 建立的语义标记能可靠对应 replay task；重复 kernel 名不会串层。
4. 实际 overlap 下可按 forward_id 取回独立记录，包含无有效行。

然后在首轮真实模型预热中核对 indexer、attention、MoE 与 HCCL 的映射。
若任务关联无法成立，该计时方案未通过；先修正具体采集接线。
不静默改成 eager，不用 host 提交时间填设备字段，不同时维护多套自动 fallback。
页计数、控制日志和离线脚本的 CPU 实施可继续；完整观测验收仍等待此关口。

图内页采集使用固定 Tensor，按 `[layer, graph_batch_width, max_logical_pages]` 存页内计数。
只为开启模式分配；按实际图宽分配，避免与 request slot 数混淆。

在 slot lookup 后取得 hit/cache-miss mask。
在 `supply_current` 后取得真正 IO miss mask及 P/D 源。
通过设备 scatter/reduction 写入页计数和 producer 相等标记。
不逐层 `.cpu()`、`.item()`、`.tolist()`。

```sequence
SR tick -> runtime: 绑定本次 batch 与请求身份
图重放 -> 图内观测缓冲: 写页计数与选择相等结果
图重放 -> 完成快照: 同流复制本次结果，保留 forward_id
完成快照 -> 既有完成事件: 排序，保护下一次 replay 复用
结果完成通知 -> 收集器: 完成后转到 CPU，记录收集耗时
收集器 -> 逐进程文件: 批量写 JSON lines
离线汇总 -> 原始时间线: 关联设备任务与生命周期事件
```

快照复用现有 `_Completion` 生命周期。
必要时沿用其图外 clone 或 pinned copy；不能仅保存会被下一次 replay 改写的 Tensor 引用。
复用图对象/缓冲前，保证旧快照已独立保存。

不为观测新加数据路径 barrier，也不关闭现有 overlap 调度。
写文件在线批量化，压缩离线做；验收 runner 在结束正常阶段前收集已写文件。
进程退出仍由父进程处理，不能依赖逐 rank 退出回执保证日志完整。

**时间表分总量、关键路径、设备忙碌三层。**

| 量 | 口径 |
|---|---|
| T_step | D 本次推进真实 decode 的 tick 入口，到该步输出处理完成 |
| token_ready | 明确采用 scheduler 处理完采样结果、输出 token 可交付的服务内观察点 |
| T_gap | 上一步 token_ready 到本次 tick 入口的有符号差 |
| TBT | 相邻 token_ready 差；非流式 HTTP 返回间隔不能替代 |
| T_tail | Main 收到 finished 到最后一个 free_ack，均为 Main 本地时钟 |
| T_free_P / D | Main 发该侧 free 到对应 free_ack，均为 Main 本地时钟 |

若 overlap 使本步在上一步输出处理前开始，T_gap 可为负。
保留 `schedule_gap=max(0,T_gap)` 与 `step_overlap=max(0,-T_gap)`。
`TBT=T_step+T_gap` 仅用于连续且已正确关联的输出步。
单请求不意味着没有调度 overlap；不把负数裁成零后仍宣称等式成立。
首个 decode、P 首 token 和 D prebuilt 分别记录。

可相加的表只使用选定关键 rank 上的互斥区间：

```text
T_step = indexer + cache_manage + miss_exposed + control_wait
       + collective_exposed + attention + moe + write_exposed
       + other + observer_exposed + residual
```

侧流 IO 的 busy 时间另列，不再整段加进 miss_exposed。
暴露 IO 是消费依赖时尚未完成的部分；按依赖边归属，不按字节比例拆时间。
未来一次多源调用无法拆时记 mixed，但源字节仍分别计数。

集合通信包含传输和等待；没有进一步证据时名称不用“纯等待”。
attention/MoE 的嵌套区间减去已独立归属的通信、读写和观测段。
并行分支取决定 join 的分支；无任务关联证据的时间进入 residual。
不能分别取各分类的跨 rank 最大值再相加。

每张表写 `clock_domain`、计量来源和覆盖率。
CPU 与设备时间的对齐只用 profiler 提供的关联；未经校准不直接相减。
分解失败、缺层或缺 rank 时，报告 incomplete，不能靠 residual 吞掉关键类别后报通过。

开销报告同时包含采集/传输/落盘成本和同用例开关差值，说明重叠部分。
不承诺数学上的零开销；关闭时无新增设备采集、buffer、profiler、日志或序列化。
必要的 Python 开关判断单独注明，通过同配置对照检查其实际影响。

## E 操作、首 token 与三项遗留

统一按因果链记录，不用一个时间戳兼任不同阶段。

| 接入位置 | 记录什么 |
|---|---|
| Main `submit/_command/_receive` | request、route、reserve、命令发送、ack接收 |
| Main `_publish_prefix/_free_after_save` | publish、free发送及对应额度归还 |
| SR `execute/advance` | 各分片开始/完成被 tick 看到、最后分片到齐、ack发送 |
| `PoolCopyExecutor.submit/_run/poll` | executor_enqueue、executor_start、poll_observed；不反复记录每次空 poll |
| `BMTransfer.transfer` | batch提交进入/返回、同执行线程 wait返回，作为 library_complete |
| PrefixStore `admit/_read/release_source` | op1/admit、op2/load_back、op3/fill 的清单、字节、源实际释放 |
| runtime `write_layer/fetch_selected_kv` | write_through 和两源读；用设备时间线，保留完成观察时间 |

为区分 C 的排队、库执行与回执延迟，BMTransfer 内必须覆盖实际 SDK 调用。
简单的条件 observer 接口即可；不重建执行器、线程池或控制回执格式。

零块分片记录 zero_work；没有调用库时 library_complete 为 null。
op2 是设备复制，不虚构 BM 库完成；用 device_complete/observed_complete 区分。
当前版本没有远端写和 replica 读；字段登记 not_applicable，不生成假操作。

字节取 CopyBlock、现有 receipt 和实际选择 mask。
逻辑字节、物理副本总字节、通信 padding 分列。
B 用例的完整回归目标：

| 路径 | 主 KV | Index K |
|---|---:|---:|
| load_back | 368,050,176 B | 22,020,096 B |
| fill | 2,944,401,408 B | 22,020,096 B |

满32页时 admit 为64个物理页块，fill为288个物理页块。
批量合并后的调用条数另计，不能替代物理页块数。

**TTFT 推荐口径：Main 感知首 token 就绪。**
本轮已发起可选澄清；截至本稿未收到回复，提案采用推荐口径。
这不记为用户已确认；切换后若有新回复，以新回复更新此处。

D 的 prebuilt/首输出就绪时，rank0 发一条只用于观测的消息。
复用 SR→Main 通道，在 Main 记接收时间，不改变原生 token 输出方式。
该消息不参与 ready、publish、释放与额度判断；关闭模式不发送。

`TTFT_main_observed = Main 收到首 token 就绪消息 − Main 收到请求`。
报告明确包含消息传递和处理延迟，另保存 D 本地就绪时间。
P 首 token 产生、D 三条件齐和 D 首步开始也各自保留。
不跨机相减 monotonic，不把非流式 HTTP 请求总时长叫 TTFT。
若用户选择实际就绪时间估计，需要另定时钟校准与误差口径后修改这一段。

三项遗留对应输出：

1. A/E：P send 完成观察、D handoff 到达观察、D 三条件齐、D 第一步 tick/forward。
   同主机局部区间分别比较，跨机只连因果；历史 5.2s 不当作必然复现值。
2. C fill：按16分片列 enqueue→start、SDK提交→wait返回、完成→poll、ack发送→Main接收。
   最后两端跨机部分仅记各自时刻；不伪造精确网络耗时。
3. 128/127：分别统计提交、完成、被消费输出的 forward。
   记录 prefill首token、prebuilt、停止原因、额外在途但丢弃输出的 forward。

P send 的第一处可复用 `defer_release(handoff=True)`，D 到达可复用 `transfer_complete`。
这些是 scheduler 观察点；若定位要求实际 transport 完成，才扩展 Ascend `conn.py` 的 poll 接口。
不把 poll 时间标成 DMA 精确完成时间。
发现 bug 另开票；未复现长尾保留为未归因，并按票要求明确转交，不能编造结论。

## F 拟改文件与主线必要性

以下是影响清单，不要求为凑清单改动每个文件。
功能源码前缀为 `python/sglang/srt/`。

| 顺序 | Ascend/NPU 功能源码 | 用途 |
|---|---|---|
| 1 | `disaggregation/ascend/mempool_router_protocol.py` | 两个观测配置字段；已有启动配置传递 |
| 2 | 新增 `hardware_backend/npu/mempool/observation.py` | 公共记录字段、进程文件 writer、主机事件与证据输出；导入不分配 NPU |
| 3 | 新增 `hardware_backend/npu/mempool/observation_device.py` | 固定图内计数、快照、模块标记及已核验的 profiler 接入 |
| 4 | `hardware_backend/npu/mempool/runtime.py` | capture 前装配、forward关联、快照保活、源mask和写入观察 |
| 5 | `hardware_backend/npu/sparsity_driven_kv_offload/manager.py` | slot lookup、命中/缺失和缓存阶段采集 |
| 6 | `hardware_backend/npu/sparsity_driven_kv_offload/attention.py` | 消费 wait、attention与回填依赖标记 |
| 7 | `hardware_backend/npu/mempool/copy.py` | 必要时标记 P/D 两次 gather 的实际源区间 |
| 8 | `hardware_backend/npu/mempool/pool_copy.py` | enqueue/start/SDK/wait/poll事件；保留原完成语义 |
| 9 | `hardware_backend/npu/mempool/prefix.py` | 操作字节与源释放；迁移已有 prefix 证据 writer |
| 10 | `disaggregation/ascend/mempool_service.py` | tick、batch、token结果、三条件到齐与请求停止摘要 |
| 11 | `disaggregation/ascend/mempool_subrouter.py` | 分片完成、ack与观测消息投递 |
| 12 | `disaggregation/ascend/mempool_router.py` | Main观测、publish、额度归还与结果关联 |
| 13 | `disaggregation/ascend/mempool_router_transport.py` | Main请求入口、本机收发时刻与纯观测消息分流 |

图入口已有 `model_forward_scope`；优先从 runtime 装配。
只有 profiler 接线确需 capture/replay task信息时，才改 NPU `graph_runner/npu_graph_runner.py`。
indexer/MoE 可在 runtime初始化时，对已加载的模块实例安装只在开启时存在的 hooks。
先核实实际 GlmMoeDsa/DeepseekV2 路径，避免为了观测修改模型数学实现。
必要 transport细化仅在 Ascend `conn.py` 内进行。

**拟修改的通用主线：`managers/scheduler.py` 一处。**
在 `process_batch_result` 的实际结果处理分支之后，增加受开关保护的结果通知。
通知转发给现有 `mempool_service`，逻辑全部留在 Ascend侧。
用于 prefill、prebuilt、decode 的输出就绪与停止状态，随后正常执行原流程。

必要性：runtime 只覆盖模型 forward，不能知道采样输出是否被 scheduler 接纳。
`advance()` 在下一轮调用，可能受排队影响；已有 output_streamer 在非流式下也不逐步发送。
这两个现有位置都不能替代结果完成边界。

行为影响：开启时记录结果/收集已完成快照；关闭时仅条件判断。
不调整调度、采样、release或模型返回值，不 monkey-patch scheduler。

此文件、理由和行为影响已在本轮实施前向用户说明；真正实现前再次核对净范围即可。
若还需其他主线文件，先说明新增必要性，不能借本方案扩大授权范围。

脚本、测试和说明另列，均放 `ascend-mempool-test/`：

- `scripts/run_mempool_min.py`：复用启动、ready、请求、故障、双端清理和归档。
- `scripts/run_k1_full_acceptance.py`：复用四组配置生成；保持旧入口可用。
- 新增 `scripts/run_k2_observation.py`：开关配对编排，阶段与覆盖范围输出。
- 新增 `scripts/verify_observation_graph.py`：无模型 Graph验证，用户自运行。
- 新增 `scripts/analyze_k2_observation.py`：四个分析子命令与 summarize，共用解析与分组函数。
- 现有 `verify_mempool_*.py`：适配统一证据文件，保留所有原正确性判据。
- `tests/unit/`：新增少量观测行为测试；修改受日志迁移影响的现有测试。
- 新增 `K2_TICKET13.md`：已实现 CLI 的双机命令、预期、失败标准和日志目录。

## G 按这个顺序实施

| 步骤 | 交付与完成判据 |
|---|---|
| 0 固定基点 | 记录当前HEAD/脏文件；保留已有资料改动；核对用户对TTFT的回复 |
| 1 数据合同与 writer | 实现默认关闭与逐进程文件；一份CPU fixture可由四项分析读取；关闭时没有采集对象与I/O |
| 2 控制与 op 链 | 接 Main/SR、executor/SDK 与结果hook；CPU因果测试通过；零工作/失败保持原语义 |
| 3 图采集可行性 | 完成无模型入口和部署版本快照；用户运行确认 replay/task映射，不启动模型做小验证 |
| 4 逐层页与时间采集 | 固定buffer、有效mask、current供给、producer比较、fork内容身份；真实预热核对完整设备分类 |
| 5 清理与验收编排 | 迁移残留输出、保留校验；复用票09覆盖与阶段清理；开关配对报告可自运行 |
| 6 CPU检查与两轴评审 | 一轮关键测试与静态检查；发现问题只对受影响项第二轮；按最终净改动评审 |
| 7 交付与NPU确认 | 提供单commit、阅读清单、每机一条命令；分析用户日志后更新13与目标页D |

步骤3的实机反馈未到时，可以完成不依赖设备计时结论的实现和CPU检查。
不能把未验证的图计时能力写成已完成验收。
每个步骤留同票内小切片，不另拆多票，也不提前开始18或22。

功能接口保持小：主机 `emit(event, keys, values)`；
设备侧围绕既有 `capture_layer / snapshot_forward / collect_completed`。
名称可随最终实现简化，不为将来多机多组增加注册中心、消息总线或插件体系。
离线分析直接消费公共schema，不导入 NPU runtime，也不加载模型。

## H 验证、开销与交付边界

CPU只保留关键行为验证：

1. 开关关闭没有观测buffer/event/profiler/file，原数据与完成路径相同。
2. 一个混合页 fixture 同时覆盖 hit、P远端miss、D本地miss、当前token直供、无效与padding。
3. 同页不同 token 能被 producer比较发现；两次快照和槽复用的身份不会串。
4. 重叠时间线的分解不双计，负T_gap可解释，跨时钟不能直接减。
5. SDK真实完成→poll→ack→publish→源释放顺序；空任务和已有故障路径不变。
6. 四分析使用小而手算得出的例子；不同内容的page0不计fork重合。

不重跑全仓库大套件。运行受改动影响的 runtime、prefix、pool-copy、Main/SR、runner 关键测试。
日志迁移的读取端必须一起验证，防止功能没变但验收器失效。
既有已确认失败沿[K1清理记录](../k1/cleanup-test-hooks.md)保留，不能报全绿。

NPU完整覆盖保留票09的四组几何与 V1–V10。
每组包含 observe关/开；保留ABCE、部分/完全命中、长生成、复用、拒绝、超时与清理。
输出数值按同输入同配置的开/关逐位比较，同时保留既有语义答案与prefix内容检查。
过去接受的不同冷热输入数值限制不扩大为严格逐位通过。

复用同一套输入与离线参考，不为四项分析各启一遍模型。
每个配置/模式最多两轮；第1轮预热，报告第2轮与实际 n。
模式改变图中采集内容，需要单独启动/capture；不在线翻一个布尔值就声称图内采集消失。

跨轮若需恢复 A 冷请求与空 prefix 池，沿现有重启流程，不新增生产清池命令。
按四组、两态、两轮完整运行，正常目标服务最多16次启动。
原生参考、容量失败和超时隔离的必要启动另列；同一参考结果按等价配置复用。

runner 输出实际 service_starts、capture_count、rounds，容量/故障隔离阶段单独列出。
这些是不同验收场景，不得借命名绕过同一测试最多两轮的约定。
四组×两态不能偷偷缩成一组；精简入口只标 smoke。

四项分析全部复用已归档真实日志。
ABCE之外若缺fork兄弟，在同一正常服务阶段补两条串行、共享prompt而后缀不同的请求。
共同prefix由输入hash决定，按相同层/步比较，不假设逻辑页号等于内容身份。
512输出只在现有 S_D=512配置中运行，不改容量约束来迁就测试。

完整报告至少包含：

- 逐进程JSONL、原始设备trace、run manifest与拓扑/版本。
- 每步分解及residual说明、开关开销、实际样本数。
- 在线/离线计数一致性、B用例字节等式、四项真实日志分析结果。
- A/E、C长尾和128/127的归因，或明确的新ticket与未验证说明。
- 本票删除/迁移清单和限定目录的rg结果；功能源码与脚本/测试/文档分列。
- Standards与Spec评审、CPU实际结果、用户NPU结果、尚未执行项目。

本轮实际工作仅为资料、源码、历史提交与官方计时接口核对，以及本文规划。
未执行CPU功能测试、未访问NPU、未提交commit、未关闭ticket。
