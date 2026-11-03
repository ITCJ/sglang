---
title: K2 交接核对与四项实施答复
subtitle: 资料已同步；以下区分源码事实、实施建议与待做的 NPU 验证
template: doc
theme: blueprint
lang: zh
date: 2026-11-03
---

K2 十三张票已导入；当前可开始 13 与 14。
本轮只更新资料并核对代码，没有开始功能实现。

## A 13：每步分解建议采用两套相连的记录

**固定一个墙钟总量，再解释关键路径；各流忙碌时间另列。**
这样 21 的预取、22 的合并加入后，重叠时间不会被重复相加。

| 量 | 建议固定的边界 |
|---|---|
| T_step[i] | D scheduler 开始推进第 i 次真实 decode（本次控制 tick 入口），到该次 forward/sample 完成、输出 token 可交付 |
| T_gap[i] | 上一步输出就绪，到本次 tick 入口；单列排队/调度间隙，防止并发时把等待藏掉 |
| TBT[i] | 相邻输出就绪时间差，等于 T_gap[i] + T_step[i]；非流式测服务内 token 就绪，不拿 HTTP 返回间隔充数 |
| TTFT | Main 收到请求，到首个 token 满足交付条件；prefill 产出的首 token 单列 |
| T_tail | Main 收到 finished，到最后一个 free_ack；沿用 K1 |
| T_free_P / T_free_D | Main 发出该侧 free，到收到对应 free_ack；沿用 K1 |

K1 旧 T_step 只写“decode 开始→结束”。本次明确 tick 与 token 就绪边界，
旧约 163 ms 不直接当新口径基线。第 1 个 decode step 单列。
分别记录 `n_prefill_forward / n_decode_forward / n_output_tokens`、停止原因、最后位置。

输出 128 个 token 不强制等于 128 次 decode；以实际事件解决旧 128/127 问题。

**可相加的主表使用关键路径上的互斥区间：**

| 字段 | 归属 |
|---|---|
| indexer | producer 的 indexer 计算；共享层记录复用关系，不重复算 producer |
| cache_manage | resolve、命中数据整理、替换/回填/slot map 中真正延长本步的段 |
| miss_exposed | 消费流到达依赖点后，仍须等 miss 数据的时间；按实际源分类 |
| control_wait | 当次 tick、控制消息和就绪门控导致的等待；不含已归入 T_gap 的时间 |
| collective_exposed | 集合通信在关键路径上的耗时，包含传输与等待；没有更细证据时不冒称纯等待 |
| attention / moe | attention 和 MoE 计算；已单列的集合通信从中扣除 |
| write_exposed | write_through 等写入依赖暴露的时间；避免把 D 每层写回归到 miss IO |
| other / residual | norm、投影、采样、输出处理与未覆盖段分开标注；残差不静默摊平 |

```text
T_step = indexer + cache_manage + miss_exposed + control_wait
       + collective_exposed + attention + moe + write_exposed
       + other + residual
```

第二张表保留每个 kernel、拷贝和通信的原始起止及忙碌时间。
侧流 miss 与计算重叠的部分属于忙碌时间，不再加进 miss_exposed。
21/22 的对齐指标使用每个真实输出 token 暴露的 miss IO。
同一批有多个请求时，共享批次耗时只记一次，记录请求列表；不把它重复求和。

多 rank 保留原始记录，以完整关键 rank 时间线解释总量，不能相加各分类的跨 rank 最大值。

源类别为 `P_remote / D_local / replica`，同时保留实际 region、机器与 NUMA。
单次多基址调用若无法拆出各源耗时，时间记 `mixed`；按源字节仍精确计。
不按字节比例虚构每个源的耗时。

实现只在观测模式开启时装配图内计数/时间点，复用实际依赖事件。
图 replay 不执行 Python 层 hook，不能用 Python 提交时长当逐层设备时间。
先验证计时点能随图重复更新；图内观测缓冲稳定，图外在已有完成点后收集。

不为计时逐层 synchronize，不在网络线程新增集合通信。
主机用 monotonic 时间，设备用设备时间线；保留 clock_domain。
P/D 单调时钟不直接相减，跨机用消息关联和各端局部区间解释。

TTFT 记录 route、reserve、load_back、prefill/chunk、handoff 和三条件到齐。
这些区间可能重叠；同样按关键路径核账，不把各段 wall time 硬加。
每 op 保留 submit、library_complete、ack、publish、source_release、credit_return。

不适用的事件留空并说明，不能拿 ack 时间补造库完成时间。

## B 13：页集合与离线分析的数据合同

**单个页可能只有一部分选中 token 命中，必须保留页内计数。**
只有“这页命中/缺失”一个布尔值，不能重算 token 缺失率。

| 层次 | JSON lines 的必要信息 |
|---|---|
| 标识 | schema_version、run、request、step、layer、rank、machine、attn_group、reader_numa、batch_id |
| 配置 | page_size_tokens、hot_cache_tokens、top_k、replacement_policy、采样范围/轮次 |
| 每页 | logical_page、稳定内容标识或 prefix hash、region/physical_page、selected_count、hit_count、miss_count、io_miss_count、miss_source 及分源字节 |
| 页面标签 | hit 表示本页所选行全部命中；miss 表示至少一行缺失，附 has_hit 及上述计数表达部分命中 |
| 选择复用 | producer_layer、与 producer 的有效 token 选择是否相同；不能仅比页集合就声称 token 选择相同 |
| 当前 token | 单列 current_device_supply；它可属于缓存 miss，但不产生主存 miss IO |

页号需带源/内容身份；不同请求的逻辑 page 0 不是同一物理页。
fork Jaccard 在共同 prefix 的内容页身份上比较，避免把不同内容误算重合。
票 19 的温度键采用不可变内容页与读者 NUMA，描述符保留机器/区域定位。

padding、无效 top-k 和补齐通信不计有效 token 或读字节。
页集合去重；token 缺失率按真实有效选择计数，物理搬运字节另外按实际传输计数。
同一页发生多源读取时按源展开，不强行只填一个来源。

```text
token_cache_miss_rate = sum(miss_count) / sum(hit_count + miss_count)
io_miss_rate = sum(io_miss_count) / sum(selected_count)
page_miss_rate  = count(至少一行 miss 的选中页) / count(选中页)
cross_step_overlap = |pages[i] ∩ pages[i-1]| / |pages[i]|
fork_jaccard = |pages[a] ∩ pages[b]| / |pages[a] ∪ pages[b]|
```

miss_count 取热缓存查询结果；io_miss_count 取实际主存读取计划。
当前 token 的本地直供属于前者、从后者扣除，不能用 IO 缺失率冒充缓存缺失率。

当前 token 的本地直供另报占比；空分母输出 null 并写明无样本。
集中度与票19温度按页累计实际 IO miss 行数/字节，两种曲线标明单位。
用户 k/2k/4k 日志直接使用同一字段合同；不只从页级布尔值推 token 缺失率。

page_size_tokens=128；hot_cache_tokens=2048/4096/8192，明确区分两种 B。

模式默认关；关闭时不建观测 buffer/event、不收集页集合、不写日志。
开启时记录自身 CPU/设备收集开销及同用例开关差异，最多两轮、首轮预热、注明 n。
既有 K1 删除项引用原清单；本票只登记实际残留的删除或迁入项。

依据：[miss 路径与事件](../../python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:876)、
[attention 消费等待](../../python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/attention.py:189)、
[当前 token 直供](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py:714)、
[K1 清理记录](../k1/cleanup-test-hooks.md)。

## C 14：基础映射和库接口已有，服务写适配尚缺

| 核对层 | 当前事实 | 14/25 要补什么 |
|---|---|---|
| BM 会话与映射 | 共享模式遍历有贡献的 P/D participants，校验设备映射；P 可拿到 D alive view | 复用，不新增一套会话 |
| 服务 UNIDEX 写适配 | MempoolKVOffload 拒绝 target.rank != owner.rank | 需要远端目标适配；当前不能直接把 target 换成 D 就运行 |
| BM 库 | v1.1.4 有 L2G、batch、wait；设备→全局主机方向有 SDMA 实现 | 可直接用于 P HBM→D 池的小程序，无需先新增 SDK API |
| 工程执行器 | BMTransfer 目前固定 G2G | 接 L2G 时需区分 HBM 指针与池 GVA，保留源 tensor 直到完成 |
| 默认容量 | K1 的 D alive 只有小生成容量 | 14 独立小程序按 4K/32K/128K 配置同布局容量，不能用默认 S_D=128 跑大写 |
| 版本 | 本地 kernel 仍 uint32；用户侧已完成 64 位 | 记录实际部署 kernel/SDK SHA，完成与可见性仍待用户 NPU 验证 |

先出的三档是每层逻辑主 KV：4K 为 4,718,592 B，32K 为 37,748,736 B，
128K 为 150,994,944 B。每档都有“只写”和“同时 D 本地读”。
page first 目标按 147,456 B 的页-层块分散在不同物理页；
测量须包含页列表与实际写者分工，不能拿连续大块峰值代替该路径。

报告区分全层总字节和每写者字节。

14 首项测 25 拟用路径：P HBM→D BM，计时从 L2G 提交到库完成。
D 随后按正常设备读路径核对；读对照复用相同版本 UNIDEX。
如果另测 UNIDEX 远端 store，单列其完成机制与结果，不能混合两条路径。

比较层计算窗口时需 13 的实测或已有同机有效样本；
14 自身不启动服务，单有 GB/s 不能证明写能藏住。
源缓冲保活和排队也计入暴露开销，不仅测 DMA 时间。

依据：[映射解析](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py:412)、
[本侧写限制](../../python/sglang/srt/hardware_backend/npu/mempool/offload.py:36)、
[现有 L2G setup 用法](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py:647)、
[G2G 执行器](../../python/sglang/srt/hardware_backend/npu/mempool/pool_copy.py:54)。
上述 setup 用法会全局同步，只证明接口已有，不能原样用于逐层 write_through。

## D 18：可以配合现有图捕获，需验证新页表的 replay

**源码结构支持这一方案；新页表还没有 NPU 验收。**
现有 runtime 已预分配 row_slot 等设备 Tensor。
bind/detach 在图外改内容，记录 _binding_update。

begin_forward 在提交 eager/replay 前等待此事件。
KVFetch 的固定输入也已通过 copy_ 更新；无须每请求重捕获。

```sequence
SR tick -> 设备页表: 原位更新已预留的页表行与版本
设备页表 -> 更新事件: 记录更新完成依赖
replay 流 -> 更新事件: wait_event
replay 流 -> 图内查表: 逻辑页转物理页与 64 位偏移
图内查表 -> UNIDEX: 固定基址内的散行读写
```

页表按请求行、最大页数预分配，地址/shape/dtype 固定。
图外仅更新内容；图内用 Tensor 查表与 64 位地址计算。
不把物理页转换成 Python 常量，不在 replay 内换 Tensor 或做 .item()。

已有读者/写者 drain 后才覆盖或复用同一页表行；更新事件只保证后读看到更新，
不能代替旧读者完成。后续 19 的替换同样遵守这个边界。

18 的两源仍分开调用：P alive 单基址 + D alive 单基址。
各自内部可散页，不要求一次调用跨区域。
25 引用多个 prefix 段时才接多基址 kernel。
因此 18 不等多基址，但要接入用户的 64 位版本并撤掉旧窗口拆分。

18 用页表版本拒绝旧更新；在提交 replay 前核对控制侧版本，图内保持无主机同步。
不能仅把过期页 mask 掉就算成功，过期路径按票要求 fail-stop。
15 引入代号后统一版本口径。

验证重点：同地址换页、零有效行、补齐行、跨 4 GiB、写读顺序与回收后复用。
不同 kernel 的具体参数契约仍需接入核对，不能由 uint64 索引类型推断完整地址链已支持。

依据：[固定缓冲与更新事件](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py:299)、
[replay 前等待](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py:497)、
[固定 gather 输入](../../python/sglang/srt/hardware_backend/npu/mempool/copy.py:242)、
[当前 32 位窗口](../../python/sglang/srt/hardware_backend/npu/mempool/indexed_copy.py:1)、
[NPU replay 入口](../../python/sglang/srt/hardware_backend/npu/graph_runner/npu_graph_runner.py:244)。

## E 25：首版建议采用库完成

**选 BM 库实际执行远端写，再用库完成确认。**
P 侧目前 eager，可以复用独立执行器；D 的图仍只消费已就绪的页。
SDK v1.1.4 的 L2G 批量方向走设备→全局主机拷贝，
wait 取调用线程对应的内部 stream。因此提交和 wait 必须在同一个执行器线程。

```sequence
P 计算流 -> 源就绪事件: 该层 KV 产生
执行器 -> 源就绪事件: 确认源可用，保持 HBM tensor
执行器 -> BM: L2G batch，P HBM 到 D 池 GVA
执行器 -> BM: 同线程 wait
BM -> 执行器: 实际完成或失败
执行器 -> SR tick: poll 返回结果
SR tick -> ready_tokens: 汇总该范围全部写者与必要写目标
```

P prefix 候选页的本机写也要完成，才能报告对应的可发布状态。
只累计已完成且连续的 token 范围；D 仍等 prompt、Index K、handoff 三条件。
失败、超时或完成未知沿现有 fail-stop，不提前复用源 HBM 或目标页。

批量子提交错误传播沿票 11 的已交付 SDK 补丁核对，不自动升级外部 SDK。

**BM wait 不覆盖 UNIDEX 直接写远端地址的操作。**
普通 torch Event 也没有在本项目中证明对端读者可见这一语义。
若后续改成 UNIDEX 直写，需要独立确认远端完成 fence/顺序协议，
并用真实 D 设备消费验证；当前未找到可直接复用的该类 fence 封装。

不能在 UNIDEX 后补一个空 BM wait 就宣布完成。

这项建议把 25 的远端写执行路径具体化为库 L2G，
不是对原 UNIDEX store 补调用。14 必须测同一路径；
是否达到层间隐藏条件、对端立即可读、错误传播均留待真实硬件结果。
不增加 P alive 中转，也不引入 D→P 整段回拷。

接口事实参考 [MemFabric 官方 Python API](https://github.com/Ascend/memfabric_hybrid/blob/master/doc/pythonAPI.md)。
本次另核对本地 SDK 的 v1.1.4（`c01f3ad842b9`）：
`smem_bm.h` 的 copy_batch/wait 声明，
`hybm_data_op_sdma.cpp` 的 BatchDataCopy、Wait、InnerWait。
它们支持接口与 stream 选择判断，不代替部署二进制的远端可见性验证。

## F 同步结果、源稿冲突与下一步

同步清单共 33 份：K2 执行文本及 13 票、K1 执行文本、16 份论文资料、两份 M60 设计原稿。
K1 本地验收勾选与 Comments 保留；04/10 按用户指令转归关闭。
目标页 D 与恢复入口已更新；K2 均 open。

需设计侧补注的主要冲突已集中在 [spec](spec.md)：

- 25 新算页读 D 本地；命中页仍引用 P prefix，不能要求 B/C 远端读为零。
- 主 KV 写字节等式仅适用于新算且保存的页；Index K 沿原生 HBM 传输。
- 15 继承 20 的多 chunk；“单 chunk 不变”是滞后文字。
- 本地已清理 K1 探针；用户 64 位 kernel 尚未在本地接入。

13 的统计口径与 25 的完成路径在本页明确为建议。
本轮没有功能源码改动，也没有新的 CPU/NPU 功能测试或验收结论。
