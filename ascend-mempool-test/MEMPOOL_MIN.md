---
title: Ticket01 实现与代码审核指南
subtitle: Main / SR、原生预留、显式释放和双机启动
template: doc
theme: blueprint
lang: zh
date: 2026-10-24
---

## A 本轮实现范围与阅读顺序

2026-10-24统一页修正见[本轮改动与验证清单](../.scratch/mempool-min/unified-page-size-change.md)。
以下34文件统计描述原ticket01交付范围；本轮功能修改为5个Ascend/NPU文件。
当前GLM-5.2 mempool-min默认且仅允许B=128，所有相关组件严格一致。

本轮实现 mempool-min ticket01 的冷请求路径。Main 是独立 CPU 进程；每侧 rank 0
scheduler 负责 SR 协调，每个 TP worker 执行本 rank 的分片操作。
代码已接入真实服务入口。NPU 测试由用户运行，ticket01 保持 open，后续票仍受验收门控。

本轮共 34 个文件：15 个生产代码文件、9 个测试文件、5 个配置与启动文件、5 个说明与进度文件。
生产代码中 11 个位于 Ascend / NPU 目录，4 个位于公共调用入口。

建议先看 `mempool_router.py` 的 submit / receive，再看 `mempool_subrouter.py` 的
execute / advance，最后看 native reservation 和四个主线接缝。
审核重点是资源何时实际取走、何时可读、何时实际归还。

```flow LR
客户端 -> Main: HTTP token IDs
Main -> SR_P: alloc / dispatch / free
Main -> SR_D: alloc / dispatch / ready / free
SR_P -> P_workers: scheduler TP tick
SR_D -> D_workers: scheduler TP tick
P_workers -> D_workers: Index K / handoff
```

启用开关是 `SGLANG_NPU_MEMPOOL_ROUTER_CONFIG`，轮次变量为 `SGLANG_NPU_MEMPOOL_ROUTER_RUN_ID`。
启动脚本自动设置这两项。旧正式 mempool 参考运行会清除这两个变量。

本票为 TP16 / DP1 / PP1、单真实请求、单 chunk、private replicas、greedy、无 radix。
P eager，D Graph 固定补齐 16 行；补齐行不占真实请求槽。
P alive Index K DRAM 与 prefix 的注册容量为零。

## B 正常路径、数据结构和 DRAM 读取

| 顺序 | Main / SR 的动作 | 可审核的完成条件 |
|---|---|---|
| 启动 | BM 映射、D Graph、原生握手完成，SR 上报全组注册 | 两侧 16 rank 的模型、布局、会话和容量相符 |
| reserve | Main 一次扣四类额度，先 D alloc，再 P alloc | SR 返回实际槽、opaque 页表、D native 行页 |
| dispatch | D 安装 P/D 绑定，再由 P 确认 | 两侧 ack 后才向原生 HTTP 创建匹配 Req |
| ready | P 写回真实主 KV，SR 汇总 ready_tokens | Main 发布可读位置，再通知 D |
| decode | D 等待 Router ready 与原生 Index K / handoff | 全组满足条件；P 返回的首 token 也受此门控 |
| native cleanup | P 可先清理 native；D 停止提交并 drain | P 写回收据保留；D 原生页行实际归还后报 finished |
| free | Main 归还 D native 额度，下发 P/D free | SR 真实归还 alive 后才回 free_ack |
| 完成 | 两侧 free_ack 收齐且 D HTTP 结果已到 | 账本归零、目录清空，然后返回结果并允许下一请求 |

| 数据结构 / 所在模块 | 内容与所有权 |
|---|---|
| `MainRouter.registrations` | 两侧完整 rank 表；实际 HBM 页、请求行、NUMA 配置、模型身份、Graph 开关 |
| `MainRouter.ledger` / `Quota` | P alive、D alive、D HBM、D row；始终满足 total = free + outstanding |
| `active` / `operations` / `history` | 当前请求、op_id 回执和最近 64 个完成请求；操作回执随历史回收 |
| `directory` | B-token 链式页哈希、父节点、child_count、writing/ready/draining、owner hold、opaque locations |
| `RouterShard` | 当前 dispatch 与 Req 的绑定；复用原有 control 的槽所有权，不另建 free list |
| `MempoolPDControl` | 原生 session / attempt / lease generation 校验和真实 alive 槽归属 |
| `NativeReservation` | D 请求行、完整非连续 token 索引、prompt 长度、生成上限及绑定 Req |
| `MempoolRuntime` | 稳定 NPU row→P/D 绑定、写回/读取事件、native 行释放后仍可用的完成收据 |

P 每 rank 的 BM 主 KV 是 BF16 `[78,16,8192,1,576]`，D 是
`[78,16,128,1,576]`。16 个 rank 各有私有副本，同一 NUMA 节点不表示共享。
DRAM 逻辑页与 native HBM 页统一使用 page_size=B，默认 B=128。4096 输入、上限 128 的请求
占 P 整槽 64 个逻辑页、D 整槽 1 个逻辑页、D 33 个 HBM 页及 1 行。
目录只发布实际 prompt 的 32 页，未写槽尾仅占额度。

**DRAM → HBM sparse 访问仍沿用现有两次 `unidexcopy` kernel launch。**

每层已有 materialize 逻辑先处理 HBM sparse-cache hit，再把 miss 按位置分为两路：
位置小于 prompt_len 的数据来自 P；生成段来自 D，其槽内偏移为位置减 prompt_len。
两路通过现有 NPU 映射直接填充 HBM workspace，随后合并并进入 attention。
Main 和 SR 不搬运主 KV。`copy.py` 与 kernel 实现本轮无改动。

新增统计只记录完成 forward 中实际提交的 P/D miss 行数，并在完成事件后读取。
它用于证明数据来源，不作为性能基准。

每个 D forward 还记录实际常驻请求数、实际执行请求数、step 和槽位关联。
验收逐 rank、逐请求、逐 step 核对两项请求数均为 1。
首 token 即结束的请求应没有 D forward，不会用 padding 行冒充真实负载。

native 预留直接调用 allocator.alloc(token_count)，保存真实非连续索引。
`req_to_token` 提前写入整个预留范围，Req 的有效 KV 长度仍只等于当前已使用部分。
接收 Index K 和后续 decode 均消费这些索引，不再向全局 allocator 申请。
最终在 drain 后一次归还完整预留，覆盖提前 EOS、实际尾页的未用部分和未用整页。
预留行会重置 sparse-cache 元数据，释放行仍经过原生 free hook。

## C 生产代码逐文件说明

下表路径均相对仓库根目录。新增文件标记为“新增”，其余为修改。

目录 `python/sglang/srt/disaggregation/ascend/`：

| 文件 | 具体改动 / 审核重点 |
|---|---|
| `mempool_router_protocol.py`（新增） | 配置合同、环境开关、模型摘要、B-token hash chain、原生签名消息序列化；固定本票布局，context 留足原生长度余量 |
| `mempool_router.py`（新增） | CPU Main 状态机：严格入口检查、串行 429、一次 reserve、全 rank ack、显式 free、超时 fail-stop、状态接口数据 |
| `mempool_router_transport.py`（新增） | aiohttp / ZMQ 适配、真实能力注册、worker SR 装配、Main 进程入口；启动时核对实际 scheduler 长度限制；客户端断开不取消资源生命周期 |
| `mempool_subrouter.py`（新增） | SR 分片和 rank 0 协调；两轮 TP gather 先统一命令再确认效果；校验 dispatch；迟到 rank 保持门控；近期重复操作幂等 |
| `mempool_native_reservation.py`（新增） | D 实际预留 native 行和全部 HBM 页、attach Req、逐 token 消费、最终一次释放；区分预留容量与有效长度 |
| `mempool_control.py` | 新增 router_owned 模式和显式 acquire/bind/ready/free；旧 DONE/CANCEL 等请求消息被拒绝；D drain 停在 ROUTER_FINISHED |
| `mempool_tick.py` | 保留原生握手和心跳；router_owned 下关闭旧自主分配、绑定、drain、释放计划，防止两套资源所有者 |
| `mempool_service.py` | scheduler 装配 SR，track 前验证 dispatch，每个 tick 推进 SR；逐 D forward 记录真实负载；native 清理选择预留释放；取消/传输失败保留资源并 fail-stop |

目录 `python/sglang/srt/hardware_backend/npu/`：

| 文件 | 具体改动 / 审核重点 |
|---|---|
| `mempool/manager.py` | 保存实际采用的 NUMA 分配节点；注册不能把 fallback 分配伪报为目标节点 |
| `mempool/runtime.py` | 可选 P/D miss 来源计数；capture 时复用稳定地址，完成事件覆盖统计快照；完成报告含来源证据 |
| `graph_runner/npu_cudagraph_backend.py` | capture 日志增加实际 batch_rows，验收区分 16 行 padding 与单真实请求 |

**主线目录仅有以下四个文件；改动必要性如下。**

| 文件 | 改动 | 为什么必须接入这里 |
|---|---|---|
| `python/sglang/srt/disaggregation/decode.py` | DecodeReqToTokenPool 暴露 alloc_rows/free_rows；已有预留绕过重复容量检查；prealloc 绑定预留 | SR 在 Req 出现前分配；原生 D 接收 Index K 的目的索引由这个公共入口决定 |
| `python/sglang/srt/mem_cache/allocation.py` | alloc_for_decode 检测 reservation 后直接消费预留 | 逐 token 分配在这里统一执行，否则仍会二次申请 HBM 页 |
| `python/sglang/srt/managers/schedule_batch.py` | check_decode_mem 使用本请求剩余预留 | 已预留页不在全局 free pool，继续按 free pool 判断会错误触发 retract |
| `python/sglang/srt/disaggregation/prefill.py` | 前移已有 service.track，先核对原始 dispatch，再创建 sender | 原生 P 会把 max_new_tokens 改为 1；若此后才核对，正常 128-token 请求会被误拒绝 |

前三个接缝只在 pool 挂有 `mempool_reservations` 时生效，该属性只由 Ascend SR_D 装配。
第四个只移动已有 mempool track 的调用时机，验证逻辑仍在 Ascend service 中。
scheduler 主循环和 server_args 均未修改。

## D 启动、验收及交付文件

目录 `ascend-mempool-test/`：

| 文件 | 具体内容 |
|---|---|
| `configs/mempool-min.example.json`（新增） | 双机、模型、NUMA 节点、端口、8192/128 容量、统一 B-token 页（当前 128）和超时示例 |
| `scripts/run_mempool_min.sh`（新增） | 加载 Ascend toolkit / ATB 环境，进入 Python 自运行入口；describe 可在 CPU 环境运行 |
| `scripts/run_mempool_min.py`（新增） | 比对双机身份，记录容量和环境，启动并检查服务。运行 reference/target 和暂停 P 的超时场景；清理后再收最终日志；失败返回非零，仅清理自身进程 |
| `scripts/verify_mempool_min.py`（新增） | 四个正常请求的全输出 IDs、32 rank、Graph、逐步负载、P/D 来源、native→free 顺序、槽复用和账本归零；独立检查超时期间没有提前或迟到 free |
| `scripts/run_service.sh` | 增加可配置端口、统一PAGE_SIZE、skip warmup、timeout 参数；新入口复用旧正式服务启动配置 |
| `MEMPOOL_MIN.md` / `MEMPOOL_MIN.html`（新增） | 本审核和操作指南，HTML 由 Markdown 生成 |

在两机已有的 Ascend 容器中使用相同代码。复制示例为 `mempool-min.json`，填写
P/D 实际地址、GLM-5.2 模型路径、网卡及在线 NUMA 节点。两侧配置内容必须完全相同。
run-id 必须相同，且对应日志目录不存在。默认 Main 在 P 的 6699 端口。

示例 context 为 8448，native 总容量为 16384 token。
原生 worker 与 scheduler 各保留一个位置，所以 context 至少需要 8322。
旧计划中的 8320 会把最大请求裁成 126 输出；已实现的配置会拒绝该值。

P 机一条命令：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode acceptance --config mempool-min.json --run-id ticket01-001
```

D 机一条命令：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode acceptance --config mempool-min.json --run-id ticket01-001
```

`acceptance` 自动运行 P-only / D-only / 混合复制适配器、同配置同长度的旧正式
mempool 参考、Main/SR 目标服务，再比对完整输出。四个请求依次为：
两个不同 4096-token 输入各强制 128 输出；B-token 输入实际提前 EOS；4096 输入仅输出 1 token。

正常请求通过后，入口执行一次独立 timeout / fail-stop 场景：

1. 保存正常日志，暂停本轮启动的 P worker 进程组。
2. 新请求让 D 实际占用 native 行页，P 的 alloc 无法完成。
3. 等待超时，检查拒绝新请求、额度不退还、目录不变化、没有 free。
4. 恢复 P，再清理本轮进程；两侧退出后再次检查完整 D 日志。

该场景默认额外等待一次 600 秒的 operation_timeout。
CPU 模拟已覆盖暂停、恢复、跨端协调和真实子进程回收；硬件结果待用户运行。

全流程失败即非零退出。Graph 未覆盖全 rank、缺少真实 D forward、未证明 P/D 来源、
资源未归还、未复用槽或输出不同，均不能作为通过。

常驻服务把两条命令的 `--mode acceptance` 改为 `--mode serve`，并换新的 run-id。
P 侧启动 Main 与 P，D 侧启动 D，SR 随 worker 加载。服务就绪后从 Main `/generate`
提交 token IDs；`/status` 可审查账本，忙时 429，注册不全或故障时 503。

两机的 runner 自动协调后续步骤。任一侧失败或退出，另一侧结束自身子进程。

可在启动前添加 `--describe` 查看配置。该模式只展示计划，不执行 NPU 验收。

主报告：`/home/cryang/log/mempool-min/ticket01-001/summary.json`。
回传整个同名运行目录，包含两机 identity、code.patch、npu-smi、meminfo、服务日志、
实际输入输出、Main snapshots、adapter 报告和 evidence.json。
target 目录另保存 `prefill-normal.log`、`decode-normal.log`、
`timeout-snapshots.json`、`decode-fault-final.log` 和 `timeout.json`。
最终 timeout 报告必须包含 `process_cleanup_verified=true`。

本轮验收额外核对32个rank的page_size/hbm_page_size注册、整槽页表和native实际预留。
PAGE_SIZE与旧HBM_PAGE_SIZE同时存在时必须相等，冲突会拒绝启动。

报告保留 `user_accepted=false`；测试脚本通过后仍需用户核对并确认验收。
NUMA 节点按配置和实际映射记录，物理页驻留未做独立测量，报告明确标为未验证。

## E 测试文件与验证边界

目录 `ascend-mempool-test/tests/unit/`：

| 文件 | 本轮新增验证 |
|---|---|
| `test_mempool_router.py`（新增） | 注册完整性、入口原子拒绝、四类额度、两侧 free_ack、重复 ack、错误和超时保留资源 |
| `test_mempool_subrouter.py`（新增） | 真实 16 对 control / TP tick；迟到 native rank 门控、dispatch 参数检查、重复 alloc 和冲突 fail-stop |
| `test_mempool_native_reservation.py`（新增） | 非连续 33 页实际占用、有效长度、提前 EOS、max=1 未用预留释放、页不足归还未发布行 |
| `test_mempool_native_hooks.py`（新增） | 执行公共接缝的真实方法体；全局空闲行页为零仍消费预留；P 参数规范化前校验；原生 scheduler 保留 8192+128 完整预算 |
| `test_mempool_min_service.py`（新增） | 32 个真实 service / runtime / control，P 真写回并先清 native，收据保留；D 首 token 门控、实际 forward 负载、零 forward 结束和显式 free |
| `test_mempool_min_entry.py`（新增） | HTTP 429、断开不释放、CPU describe、参考配置、完整 IDs 与实际 EOS；逐步负载证据；超时暂停恢复、两侧清理和迟到 free 拒绝 |
| `test_pd_tick.py` | 新模式不自主分配和释放，旧消息隔离、显式 free；同时保留旧模式回归 |
| `test_fetch_layers.py` | 78 层 P/D 来源计数、prompt 边界、生成段零偏移、padding 和复用 |
| `test_materialize.py` | native 预留行复用后必须重新取第二请求的数据，不能命中第一请求的 sparse cache |

CPU 测试替代设备、通信和 allocator 边界，验证状态与资源生命周期。
native hook 测试沿用现有 AST loader，执行原方法体，避开 Mac 上无法加载的 NPU 模块。
这些结果不能代替 NPU kernel、Graph、MemFabric 可见性或模型输出验收。

本地完整 CPU 测试命令（依赖包括 torch/msgspec/aiohttp/pyzmq/numpy/transformers）：

```bash
PYTHONPATH=ascend-mempool-test/src \
  python -m unittest discover -s ascend-mempool-test/tests/unit -v
```

完整 CPU 套件 408 项全部通过，耗时 24.358 秒。
首次运行缺少 transformers；补齐仓库指定的 5.12.1 后完整重跑通过。

15 个相关源文件 strict mypy、26 个修改 Python 文件 AST 和 lint 已通过。
mypy 使用本机 Python 3.12 配置；transformers 的缺失导入仅在本机检查配置中忽略。
lint 只豁免 `schedule_batch.py` 原有 ForwardBatch 重复导入的 F811；本轮未改该导入。

两个 shell 入口的语法检查、启动 dry-run、git diff whitespace 检查均通过。
HTML 从 Markdown 生成，两个页面的 STE 检查均无警告。

## F 进度记录与审核边界

`.scratch/mempool-min/issues/01-cold-request-lifecycle.md` 追加本轮实施和待验收记录。
`.scratch/mempool-min/target-alignment.md` 的 D 面板随实现步骤更新。
`.scratch/mempool-min/target-alignment.html` 从对应 Markdown 同步生成。
这些进度记录区分代码实现、CPU 检查和用户已接受的 NPU 证据。

发生错误或超时后，Main 停止新操作，worker 不走旧取消回滚，不把进程退出视为 drain。
资源保留到本轮进程结束，入口收集失败报告并清理自身进程。客户端断开同样不能证明完成。
本票未启用 prefix、load-back、admit 或共享副本，也不解锁下一票。
审核后请按双机命令取得真实硬件证据，再决定是否接受和关闭 ticket01。

## G Standards

硬性规范问题已解决，无新增阻断项。
Quota 与 NativeReservation 已改为 msgspec.Struct，符合贡献指南要求。
prefill.py 的调用前移范围合理，Ascend 专用验证仍由 service 承担。

保留两项非阻断维护性建议：

- `_receive`、`advance`、`stage`、`check_target` 较长，可后续拆出专用 helper。
- `MainRouter.active` 使用字典与字符串状态，可用内部类型容器增强静态检查。

规范轴剩余 2 项维护建议，最高为函数长度建议；没有未解决的硬性违规。

## H Spec

最终复核没有未解决发现。已修复并复核：

- P 参数校验先于 max_new_tokens=1 规范化。
- 每个 D forward 都有真实常驻数和执行数证据。
- 独立 timeout 场景检查 fail-stop，并在两侧退出后核查完整日志。
- 8192+128 请求保留完整生成预算，启动时检查实际原生限制。

规格轴剩余 0 项代码发现。NPU 验收仍待用户执行并确认。
