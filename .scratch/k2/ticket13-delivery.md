---
title: K2 ticket13 观测模式交付
subtitle: 代码与 Mac 验证已完成；等待用户 NPU 验收
lang: zh
template: doc
theme: blueprint
date: 2026-11-03
status: open；18 / 21 / 22 未解锁
baseline: dc31b37cbefbb0b376997fd3949b94a01cb3fdca
---

默认关闭的观测模式已接入 K1 正常路径。开启后记录设备页计数、Main/SR/SDK
生命周期、逐步时间与停止边界，提供四项离线分析和配对验收入口。
本页是实现交付；没有运行 NPU，没有关闭 ticket13。

## A 正常执行路径与所有权

`RouterConfig.observe=false` 时不创建 Observation、页计数 Tensor、profiler 或 JSONL。
原有 prefix 正确性校验由 `verify_prefix` 独立控制。
新增 Python 开关判断保留；实际开销以同配置开关对照报告为准。

开启后，每个 Main/worker 单独写 `observe/<role>-rankNN-PID.jsonl`。
Main 继续持有 Router 账本，SR 继续在既有有序 tick 执行状态迁移。
executor 在线程内调用实际 SDK batch / wait，分别记录库完成与 tick 看到完成。
新增观测消息使用原有通道；没有新增 collective 或网络线程状态迁移。

图内只操作固定 Tensor，从实际有效选择、命中 mask 与 current-device supply 后的 IO mask
累计每页计数。每次 forward 在既有完成事件前保存独立快照，完成后才转到 CPU。
不逐层同步，不用 Python hook 用时冒充设备时间。

producer 比较有序有效 token 选择；共享层自己的命中、IO 计数仍独立保存。
不对齐 prompt 引起一逻辑页跨两张 D 物理页时，分别保留源计数与位置。

设备 profiler 从 capture 前一直保持到正常阶段结束。捕获的 MSTX 语义范围随图重放，
动态外围范围关联 forward_id；实际工具链是否保留两次独立范围，由无模型 gate 检查。
本机未证明此能力。旧 msprof 可能合并 replay 记录，gate 缺数据即失败。

测试阶段空闲后，通过原有 tick 停止 profiler、导出并 flush。
这用于正常测试阶段收集；进程仍由既有父进程清理路径结束。
它不参与 free/所有权，不是逐 rank shutdown 释放回执。

## B 数据与时间口径

| 数据 | 实现口径 |
|---|---|
| 页 | 内容 hash / 生成页身份、逻辑与物理位置、源主机/NUMA/副本组；同页可部分 hit、多源 |
| 缓存 miss | selected = hit + miss；miss = IO miss + current supply |
| 实际字节 | 页 IO 与原 source counter 核账；G2G CopyBlock 与 receipt 核账，再与票09功能 receipt 对比 |
| 本地策略 | 实际 hot_cache_tokens；replace_selected，不冒充 LRU 或三档缓存已接入 |
| T_step | D 当前推进 tick 入口到原生结果处理后的 token_ready |
| T_gap / TBT | 保留有符号 gap 与真实相邻 token_ready 差，允许 overlap |
| TTFT | Main 收到 HTTP 请求到收到 D 首 token 就绪消息，含该观测消息延迟 |
| TTFT 阶段 | Main route/alloc/load_back/dispatch 往返；P prefill→handoff；D handoff→三条件→首输出，各用本地时钟 |
| tail/free | Main finished→最后 free_ack；每侧 free 发送→对应 free_ack |
| op | enqueue、executor_start、SDK submit/return/wait、poll、shard、ack、publish、source_release、credit_return 分开 |
| 停止 | prefill/decode 提交数、结果完成数、丢弃输出数、prebuilt、输出数、停止原因与末写位置 |

设备区间按 stream-bound MSTX 范围解释。主机与设备通过 profiler 内的 host mark
做对齐并报告 anchor 不确定度；P/D 主机时钟不直接相减。

主流 hit 等待和后续 miss 等待分开；miss 只记等待区间内实际 P/D 拷贝子范围。
observer IO 单列，侧流 busy 与暴露时间分开。

关键 rank 按完整 T_step 选择，所有分类取该 rank 的整条时间线，不相加各分类的跨 rank 最大值。
未覆盖的主机排队、采样和输出处理进入明确标注的 residual。
缺 rank、缺层、缺输出步计时、缺 HCCL 任务、缺/错核账都会产生 incomplete。

NUMA 来自配置与注册，不能解释成物理驻留已验证。
当前没有 replica 读与远端写，记录 not_applicable。
独立 B=k/2k/4k 日志由用户并行分支提供；本地不制造三档容量数据。

## C NPU 自运行入口

前置沿票09：两机相同本票代码、GLM-5.2 权重、PyTorch/torch_npu/CANN/kernel、
已交付的 MemFabric batch/wait 版本与相同配置。配置中的 P_HOST、D_HOST、
model_path、网卡与 NUMA 必须为本次真实环境；SDK 参数填部署 build SHA。

平台版本、代码 diff/未跟踪文件 hash、模型标识沿用原 preflight 归档。
不要额外启用另一份 graph profiler；使用当前正常 NPU 环境与原 launcher 自动准备流程。

复制 `ascend-mempool-test/configs/mempool-min-full.example.json` 为实际配置，
例如 `/home/cryang/k2-observe.json`。P 先启动，D 随后启动，使用同一新 run-id：

```bash
python ascend-mempool-test/scripts/run_k2_observation.py --role prefill --config /home/cryang/k2-observe.json --run-id k2-ticket13-001 --sdk-build-id <DEPLOYED_SDK_SHA>
```

```bash
python ascend-mempool-test/scripts/run_k2_observation.py --role decode --config /home/cryang/k2-observe.json --run-id k2-ticket13-001 --sdk-build-id <DEPLOYED_SDK_SHA>
```

每机一条命令自动执行：无模型同图两次 replay gate →
票09四组 `(page_size,S_D)=(128,128)/(128,512)/(64,128)/(64,512)` →
每组 off/on → 原生参考、目标预热、目标测量、容量拒绝与故障清理。
每态目标最多两轮，首轮预热、测量 n=1；同次服务末尾追加不同 suffix 的 fork 兄弟请求。
六例票09证据先冻结，fork 独立保存，不改变原验收器的输入清单。

开销对照报告 HTTP 请求 wall time 的 off/on 与差值，说明含 profiler、采集与落盘影响。
raw observer_collect 与设备 observer 范围可进一步定位自身成本。
完整入口每侧正常目标服务启动16次；原生参考、组件、容量拒绝与故障场景另计。
本票没有缩减票09四组覆盖；不为 fork 额外加载模型。

`--describe` 只查看展开配置；`--smoke` 只跑首组，不能代替完整验收。
任一功能、图记录、观测完整性、输出ID或字节核账失败，入口非零退出。
成功状态 `passed_current_configuration` 仍不等于用户已确认或三档容量验收完成。

日志根目录为 `/home/cryang/log/k2/<run-id>/`。

回传两侧：
`report-<role>.json`、`graph-<role>/graph-observation.json`、每组 `v1-v10.json`、
`target/observation*.json`、`target/observe/*-summary.json/.md`、
`target/observation-fork.json`、identity/platform-versions，以及失败时对应 raw JSONL 与 profiler。
原始 trace 与大页日志留各自主机；peer 只传小检查摘要。

## D 离线分析

正常入口自动按 worker 顺序生成四项输出、JSON 和 Markdown 表。
可独立读取用户三档同格式日志；同一请求的 request_tracked 和 layer_pages 一起保留，
或每条 layer_pages 直接携带 page_hashes。

```bash
python ascend-mempool-test/scripts/analyze_k2_observation.py miss-rate <LOG_K> <LOG_2K> <LOG_4K> --output miss-rate.json
python ascend-mempool-test/scripts/analyze_k2_observation.py concentration <OBSERVE_DIR> --output concentration.json
python ascend-mempool-test/scripts/analyze_k2_observation.py cross-step <OBSERVE_DIR> --output cross-step.json
python ascend-mempool-test/scripts/analyze_k2_observation.py fork-overlap <OBSERVE_DIR> --output fork-overlap.json
python ascend-mempool-test/scripts/analyze_k2_observation.py summarize <OBSERVE_DIR> --output observation-summary.json
```

跨步只比较相邻真实 step；fork 只比较共同不可变 prompt，按相同 decode step/层。
分组保留页大小、实际缓存容量与策略，不混合不同配置。
空分母为 null。只有真实 NPU 日志才能满足票据的真实输出要求。

## E 最终源码阅读顺序

以下只列功能源码，按正常执行与数据流阅读。Ascend/NPU 专用代码：

| 顺序 | 文件 | 用途 |
|---|---|---|
| 1 | python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py | 默认关开关与采样合同 |
| 2 | python/sglang/srt/hardware_backend/npu/mempool/observation.py | 单调时钟、逐进程JSONL与共用正确性文件writer |
| 3 | python/sglang/srt/disaggregation/ascend/mempool_router.py | Main route、控制回执、发布、额度归还 |
| 4 | python/sglang/srt/disaggregation/ascend/mempool_router_transport.py | Main HTTP首边界、消息收发、正常阶段导出 |
| 5 | python/sglang/srt/disaggregation/ascend/mempool_subrouter.py | shard完成、ack与有序tick导出接线 |
| 6 | python/sglang/srt/disaggregation/ascend/mempool_service.py | 请求/step身份、结果与停止边界、handoff/三条件 |
| 7 | python/sglang/srt/hardware_backend/npu/mempool/runtime.py | 实际forward、完成快照、计数与位置归并、启用装配 |
| 8 | python/sglang/srt/hardware_backend/npu/mempool/observation_device.py | 固定页计数、producer比较、MSTX与profiler/模块/通信范围 |
| 9 | python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py | cache管理、hit/miss/refill侧流范围与命中处采样 |
| 10 | python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/attention.py | hit/miss依赖分开、attention与refill等待 |
| 11 | python/sglang/srt/hardware_backend/npu/mempool/copy.py | P远端/D本地实际拷贝子范围 |
| 12 | python/sglang/srt/hardware_backend/npu/mempool/pool_copy.py | 实际SDK线程提交/完成、排队与poll事件 |
| 13 | python/sglang/srt/hardware_backend/npu/mempool/prefix.py | op字节/计划、源释放、正确性writer迁移 |

通用主线只有 `python/sglang/srt/managers/scheduler.py`：在原生 result 分支完成后通知
观测器，结束 token_ready。NPU attention hook 早于采样/输出处理，不能提供该边界；
实施前已单独说明并获本轮实现授权。关闭观测时只判断；不改变采样、停止或资源释放逻辑。

脚本、测试与说明分列：

| 文件 | 用途 |
|---|---|
| ascend-mempool-test/scripts/verify_observation_graph.py | 无模型双replay关口 |
| ascend-mempool-test/scripts/analyze_k2_observation.py | 四项分析、设备/主机分解、完整性与字节核账 |
| ascend-mempool-test/scripts/run_k2_observation.py | 双机四组开关、结果/字节/开销报告 |
| ascend-mempool-test/scripts/run_mempool_min.py | 复用原runner，增加预热、fork及正常阶段导出 |
| ascend-mempool-test/tests/unit/test_observation.py | 默认关闭、页/位置、时钟、结果与SDK因果、分析合同 |
| ascend-mempool-test/README.md | 新入口指向本页 |
| .scratch/k2/graph-observation-research.md | 官方接口核查与旧解析限制 |
| .scratch/k2/verification/ticket13-20261103/ | 实际CPU、静态、评审与清理记录 |

## F Mac 实际检查与评审

一次完整 CPU 回归563个方法：535直接通过，22个受沙箱或本票初版问题影响的方法在第二轮全部通过；
剩余6个是已记录的基线问题。它们涉及旧uint32上限、已删available_port mock和10000端口期望，未为本票修旧测试。

新默认关闭、页计数、离线算法、SDK因果等已通过；评审后新增时钟/覆盖与交付合同 smoke 也通过。
同一测试最多两轮，未重复运行完整套件。详见 checks.json 与各CPU记录。

严格Linux类型检查、ruff、isort、Python3.9语法、diff whitespace与CLI展开检查完成后记入 checks.json。
Standards复核0项未解决；Spec初审6项修复，复核0项未解决。
评审发现的 hit/miss 误归因、缺rank误报、核账缺失、TTFT阶段、初始化流与fork均已处理。

残留核查：K1人工延迟、临时探针与旧搬运计时已在基点删除，沿用既有清单，不重复记功。
本票迁移 prefix payload 文件writer；保留验收器需要的短marker、真实内容比较和协议写计数。
启动64B映射身份probe及合法padding sentinel属于正常正确性逻辑，保留。
没有恢复人工delay。检索与逐类说明见 cleanup-audit.md。

Graph导出是否正确、真实模型逐层计时/HCCL、输出逐位一致、票09字节、开销、
三档缓存与四项真实分析输出均等待用户NPU结果。
13仍open；18、21、22不解锁。
