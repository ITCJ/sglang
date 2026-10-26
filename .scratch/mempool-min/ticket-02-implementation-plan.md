---
title: Ticket02：prompt prefix 保存代码计划
subtitle: 在已验收 cold lifecycle 上增加 Index K 写回、独立 prefix 池与异步 admit
template: doc
theme: blueprint
lang: zh
date: 2026-10-26
status: 规划已执行；实现与 CPU 结果见 ticket02-delivery，NPU 待验收
baseline: 7275f29a05 + 当前文档工作区
---

**预计修改 8 个现有功能文件，新增 1 个 prefix.py。**
这些功能文件全部位于 Ascend/NPU 目录。
计划复用既有 attention hook、service 释放门控和原生 warmup。

依据：[ticket02](issues/02-prompt-prefix-save.md)、[规格](spec.md)、
[逐票规划](ticket-plan.md)和[硬件验收流程](../ascend-mempool/verification.md)。
ticket01 已按用户确认关闭，验收版本为 `7275f29a05`。
旧 ticket02 提交保持撤回；本文依据当前代码重新规划。
下文记录实施前计划，字段、接口和命令的最终形式见交付说明。

**2026-10-26 执行记录：** 下文保留实施前规划。
代码与 CPU 验证现已完成；实际文件、接口、命令和剩余项以
[ticket02 交付说明](ticket02-delivery.md)为准。

## A 功能文件与建议 review 顺序

路径前缀分别为 `python/sglang/srt/disaggregation/ascend/` 和
`python/sglang/srt/hardware_backend/npu/mempool/`。
下表只列功能文件；顺序先定义合同，再看存储、完成条件和请求编排。

| 顺序 | 文件 | 具体修改与必要性 |
|---|---|---|
| 1 | [ascend/mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | 增加 prefix 容量与显式内容验收配置；约定 admit 描述符和完成收据 |
| 2 | [npu/mempool/layout.py](../../python/sglang/srt/hardware_backend/npu/mempool/layout.py) | 定义 P alive Index K、prefix 主 KV、prefix Index K 三个区域及偏移；更新 probe 与贡献大小 |
| 3 | [npu/mempool/config.py](../../python/sglang/srt/hardware_backend/npu/mempool/config.py) | 扩展 make_mla_layout，根据实际模型几何和启用容量构造新增区域 |
| 4 | [npu/mempool/manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 扩展带区域偏移的 typed view；映射检查覆盖新增区域每层首尾 |
| 5 | `npu/mempool/prefix.py`（新增） | 集中处理本地 prefix 页占用、BM 到 BM 复制、设备事件及请求结束后的收据清理 |
| 6 | [npu/mempool/runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 在既有 write_layer 中写回真实请求 Index K；完成条件覆盖主 KV 和 Index K；启动时创建 prefix store |
| 7 | [ascend/mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 执行异步 admit，统一目标页分配，汇总 TP16 完成；阻止过早 free |
| 8 | [ascend/mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 保留 prefix 目录，选择缺失页，建立源锁，处理 admit ack、publish 和最终归还 |
| 9 | [ascend/mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 将 store 接到 SR；注册并校验真实区域、容量、副本与验收配置 |

拟增加 `prefix_pages=0` 默认值，保留已验收冷请求配置。
ticket02 启用值为 40；当前固定规格只放行 0 或 40。
拟增加 `verify_prefix=false`，内容验收模式显式开启。
容量和验收配置通过现有 Router JSON 传入，保持原生 warmup 开启。

目前没有需要修改公共功能代码的证据。
已存在的接线可以覆盖新增行为：

| 现有接线 | 如何复用 |
|---|---|
| [Ascend forward_extend](../../python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py:1353) | top-k 已经产生，此处的 runtime.write_layer 可以读取已写入原生池的 Index K |
| [NPU Index K 读写接口](../../python/sglang/srt/hardware_backend/npu/memory_pool_npu.py:801) | 使用 producer 层号及 out_cache_loc 读取真实物理 token 行 |
| [service 完成观察](../../python/sglang/srt/disaggregation/ascend/mempool_service.py:480) | 继续通过 runtime.writes_done／prompt_ready 门控 native release |
| [service 空闲推进](../../python/sglang/srt/disaggregation/ascend/mempool_service.py:682) | control 持有请求直到显式 free，期间 scheduler 继续推进 SR |

因此预计无需改模型、公共 scheduler、native allocator 或 HTTP 启动代码。
实施若发现新公共 hook 确有必要，先记录现有 hook 不足的代码证据，再收束改动。

## B 存储区域与容量

保留 P／D 私有副本、layer-first、统一 B=128 和 alive 整槽分配。
prefix 真正按页分配；主 KV 与 Index K 共用一组 prefix 页号。
每份逻辑容量为 40 页，各 rank 的 16 份副本不叠加为 640 页内容。

| P 每 rank 区域 | 几何 | Payload |
|---|---|---|
| 现有 alive 主 KV | 78 层 × 16 槽 × 8192 token × 1152 B | 11232 MiB |
| 新增 alive Index K | 21 层 × 16 槽 × 8192 token × 256 B | 672 MiB |
| 新增 prefix 主 KV | 78 层 × 40 页 × 128 token × 1152 B | 438.75 MiB |
| 新增 prefix Index K | 21 层 × 40 页 × 128 token × 256 B | 26.25 MiB |

P payload 合计 12369 MiB，probe 位于全部区域之后。
按现有 1 GiB 分配对齐，P 从 11 GiB/rank 增为 13 GiB/rank。
TP16 的 P BM 贡献为 **208 GiB**，D 仍为 **16 GiB**。
这些是 BM 分配量，不包含模型、进程和验收参考缓冲。

P/D 启动使用相同 PoolLayout 签名，包含 P 的新增区域。
D 只贡献自己的生成主 KV 区域；不新增 D Index K DRAM 写回。
注册分别报告全局布局和本侧实际区域，校验 TP16、容量与四类副本。

runner 从同一布局计算容量，替换现有硬编码的 P 176 GiB 检查。
实际 NUMA 页落点仍须单独取得硬件证据。

复制复用现有 UniDexCopy 的双 raw-pointer 模式。
本地[算子实现](../../../sgl-kernel-npu/csrc/sparsity_driven_kv_offload/unidex_copy/op_host/unidex_copy.cpp:50)
支持独立的 src_ptr／dst_ptr，并在当前 NPU stream 提交。

worker 使用已验证的设备地址；Main 只处理不透明描述符。
不通过 CPU 指针解引用 BM 设备映射。
实际双指针复制内容与完成可见性由本票 NPU 验收验证。

## C 先让 Index K 成为可靠的保存源

runtime 初始化时取得原生 NPU KV pool，并校验实际 producer 层列表。
当前模型为 `(0, 1, 2, 6, 10, ..., 74)`，共 21 层，128 维 BF16、无 scale。
`write_layer` 只对 producer 层新增 Index K 写回。

```flow LR
原生IndexK写入 -> attention已有hook: producer 已完成提交
attention已有hook -> runtime: 真实绑定与 out_cache_loc
runtime -> P_alive: 主KV及IndexK写回
P_alive -> forward事件: 两类写入均已提交
forward事件 -> 完成收据: 事件完成且逐层计数正确
完成收据 -> native释放门控: 仍需原生传输完成
```

从原生池按真实 `out_cache_loc` gather，支持非连续物理行。
沿用已有 row／slot／position 有效掩码，新增独立 Index K 计数。
事件覆盖主 KV 与 Index K 的所有提交，并保留临时张量到设备完成。
漏 producer、少写或无效位置不能发布完整 prompt ready。
`writes_done` 和 `detach_row` 同时受两类写入完成约束。
service 因而继续按既有条件释放 HBM／请求行。

fake warmup 的预期写入为空，Graph padding 没有真实绑定。
新增 Index K 路径必须跳过这些行，不读取无效物理地址。
P native 释放之后，admit 只从 alive DRAM 读取。
复制身份与完成收据按 rid／attempt／op_id 保留，独立于已归还请求行。

## D admit、publish 与 free 怎样接起来

Main 保留现有页哈希与命名空间种子。
目录中同一哈希分别保存 alive 位置和 retained prefix 位置。
位置分别记录状态、owner hold 和操作锁；新冷请求不能覆盖已有 ready prefix。
父关系和 child_count 只随真实目录增删更新，重复冷算不重复增加。
本票继续固定 `hit_len=0`。

```sequence num
SR_D -> Main: finished，D 已 drain 并清理 native
SR_P -> Main: native_freed，可先于 finished 到达
Main -> Main: 两项齐全，选择缺失页并保护源及父路径
Main -> SR_P: admit，携带请求身份、op_id 与源描述符
SR_P -> P分片: 统一目标页号，提交主 KV 与 Index K 复制
P分片 --> SR_P: 各 rank 的目标可读收据
SR_P --> Main: 收齐 TP16 后 admit_ack
Main -> Main: 校验收据，先父后子 publish，解除操作锁
Main -> 两侧SR: 显式 free
两侧SR --> Main: 实际归还后 free_ack
Main -> Main: alive 额度归零，prefix 保留，允许下一请求
```

**Main 的必要变化**

- finished 与 P native_freed 分别留证，两种到达顺序都可推进。
- finished 时归还已实际释放的 D 原生额度；alive 等独立 free_ack。
- 跳过 ready prefix，容量不足时保留连续可发布范围，从尾部缩短。
- 对待复制源先加锁，再撤 owner hold：源引用为 1 → 2 → 1 → 0。
- 保护所需父路径；ack 后按父页先于子页的顺序发布。
- 校验源绑定、目标页号、区域、rank、层覆盖和设备完成身份。
- 相同 ack 幂等，冲突 ack fail-stop；每个 op_id 只解锁一次。
- `_complete` 只要求请求资源归还，允许 prefix 继续占用页额度。
- 保留 ready prefix 目录，清除本请求 alive 位置及无用节点。

没有待保存页时直接进入既有显式释放路径。
首版让两侧 alive 都在 admit 完成后 free，保持串行结束路径简单。
请求结果返回及下一请求还须等待两侧 free_ack 与后端输出。
Main trace 先记录收到的事件，再记录由它触发的命令，支持因果核对。

**SR 与 prefix store 的必要变化**

`prefix.py` 集中管理页占用、操作状态、复制事件和目标可读收据。
拟定小接口包括空闲页快照、幂等 admit/poll 和源释放检查。
操作记录使用有名字段：请求身份、槽、源页、目标页、event、临时张量与收据。
请求实际 free 后清理操作记录；已保存 prefix 页继续保留。

每次 scheduler tick 仍使用现有两次 TP gather。
第一次汇总包括空闲 prefix 页，协调者据此统一目标页号。
各分片验证并占用同一页集合，随后各自提交本地复制。

设备未完成返回 pending；已完成分片缓存收据，下一 tick 不重复复制。
第二次 gather 只有收齐全部 rank 才共同移除队首命令并确认 op_id。
rank 0 在此之后发送 admit_ack，避免快慢 rank 的 pending 队列分叉。

free 在本地再次检查源没有未完成复制，然后调用现有 `control.router_free`。
错误或超时保留 writing 目标和受保护源，沿用现有 fail-stop。
本票不提供失败重试、evict、load_back 或 fill；后两项属于 ticket03。

## E 测试修改与验收证据

测试沿用规格已经确认的接口，按行为逐条 red → green。
CPU 只替换设备、传输和 collective 等外部条件。
Main、SR、runtime、真实资源操作与完成判断使用生产代码。

| 测试位置 | 新增或扩展的行为 |
|---|---|
| `tests/unit/test_mempool_router.py` | 持久目录、源锁、父路径、重复 ack、部分容量、满容量与输出等待 |
| `tests/unit/test_mempool_subrouter.py` | 延迟最后一个 rank，所有队列一致等待；未完成时不能 free；重复命令不重复复制 |
| `tests/unit/test_mempool_min_service.py` | fake warmup → 真实请求 → native 先释放 → admit → alive free → prefix 留存 |
| `tests/unit/test_runtime.py` | 真实物理 Index K 索引、21 层完成条件、fake/padding 掩码和行复用 |
| 新增 `tests/unit/test_prefix.py` | 通过页操作接口检查真实内容、源与目标不别名、事件完成、重复操作及失败留存 |
| 现有 layout／pool／config 测试 | 新区域地址、probe、容量与映射检查；默认关闭配置兼容 |
| 现有 entry／native hooks／HTTP 测试 | 新模式编排与清理，warmup、真实预留拒绝及启动等待继续通过 |

以上路径均位于 `ascend-mempool-test/`。
验收脚本预计修改 2 个文件、新增 1 个文件：

| 文件 | 修改内容 |
|---|---|
| [scripts/run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 增加 prefix-acceptance 模式，生成并记录有效配置，复用双机启动、就绪、参考与清理 |
| [scripts/verify_mempool_min.py](../../ascend-mempool-test/scripts/verify_mempool_min.py) | 复用 Graph、输出、释放和资源检查；按模式区分 alive 归零与 prefix 留存 |
| 新增 `scripts/verify_mempool_prefix.py` | A/B/C/E 输入、prefix 内容和目录页号校验、事件顺序及报告完整性 |

拟使用 `--mode prefix-acceptance` 自动启用 40 页和内容验收。
`--mode acceptance` 保留 ticket01 的冷请求验收含义。
常驻 `--mode serve` 从 JSON 读取 prefix 配置，内容验收默认关闭。
共享 shell 入口直接透传已有参数，预计无需修改。
README 只补充新模式、容量、命令和回传材料。

**内容验证链**

验收模式逐层保存实际计算值的 CPU 参考，避免在 NPU 累积整段克隆。
主 KV 按 compact 576 维规范化；Index K 按 producer 层和逻辑 token 排列。
在以下三个时点逐逻辑 token 比较值，并记录 rank／层／页号：

1. 写回完成后，比较原始计算值与 alive 内容。
2. P native 释放且 admit 完成后，比较 prefix 内容。
3. alive 实际 free 后，再次读取并比较保留内容。

验收参考只保留当前请求及已发布的 40 页，过期内容及时释放。
逐值比较结果之外可记录逐页摘要，关联后续 C/E 的内容不变性。
报告判定放在验收脚本，运行时只在显式验收模式采集所需内容与证据。
该模式的额外读回和同步不用于性能结论，容量检查单列验证缓冲。

| 冷算请求 | 新增 prefix 页／份 | 最终页数／份 | 核对重点 |
|---|---|---|---|
| A：4096 token | 32 | 32 | 首次完整保存，释放后仍可读 |
| B：A 加 1024 token | 8 | 40 | 复用目录中的 32 页，只保存缺失部分 |
| C：与 A 相同 | 0 | 40 | 不重复分配或写入 |
| E：不同的 4096 token | 0 | 40 | 满容量跳过，alive 槽复用和输出正确 |

四个请求都保持 hit_len=0，并比较各自完整冷算参考输出。
CPU 另测剩余容量不足以容纳全部缺失页时的尾部缩短。
证据负向测试包括缺 rank、错误页号、内容差异、过早 ack/free 和缺少完成记录。
源槽必须与实际 alloc 对应，目标页必须与 publish 目录对应。
失联或失败仍要求非零退出和本次进程清理，不制造 free_ack。
硬件入口按每机一条命令交付，日志保存在 `/home/cryang/log/`。

## F 实施切片与交付

以下是同一 ticket 内的连续实现切片，不新增依赖票。
每片完成后立即更新[进度页 D](target-alignment.md)，同步 Markdown/HTML。

| 顺序 | 行为切片 | 完成判据 |
|---|---|---|
| 1 | Index K 写回并保持 warmup 隔离 | producer 完整写回后才可释放 native；fake 不建立绑定或改变计数 |
| 2 | 一个真实冷请求保存后释放 | 全 TP 复制完成后 publish；native/alive 释放后 prefix 仍正确 |
| 3 | 重复保存、容量与故障 | 32→40→40→40；部分容量缩短、迟到 rank、重复 ack 和 fail-stop 正确 |
| 4 | 同一入口的保存验收 | 自动参考／目标、内容证据、输出、报告与自身进程清理齐全 |
| 5 | 全回归、两轴 review 与独立提交 | 适用 CPU／静态检查通过，Standards 与 Spec 问题处理完毕 |

每片围绕可观察行为补一个失败测试，再完成对应最小改动。
最终运行完整 CPU suite、strict mypy、仓库 lint/format、语法及 CLI 检查。
本轮只阅读代码、核算容量和写计划，没有运行新增功能测试。

交付时给出实际功能文件 review 顺序、改动理由及独立 ticket02 commit。
如实际范围偏离 A 表，说明新增文件或公共代码改动的必要性。
当前基线的用户文档改动保留，提交仅包含本票所需内容。
用户完成本票 NPU 验收前，ticket02 保持 open，ticket03 继续等待。
