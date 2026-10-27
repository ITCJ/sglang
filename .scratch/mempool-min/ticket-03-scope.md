---
title: Ticket03 要实现什么
subtitle: 从已保存的 prefix，走到真实命中、后缀计算与 D 生成
template: doc
theme: blueprint
lang: zh
date: 2026-10-26
status: 范围核对；尚未实施
---

**ticket03 让下一次请求真正复用 ticket02 保存的 prompt KV。**
P 恢复命中段并只计算后缀。D 从新请求自己的 alive 读取完整 prompt。

## A 当前处于哪一步

按 `ask-matt`，已有 spec 和独立 ticket，可以先核对范围，再进入 `/implement`。
只有需要改变已定需求时，才回到 `/grill-with-docs` 讨论。

| 任务 | 当前基础 | 状态 |
|---|---|---|
| 01 | 真实冷请求、D Graph、显式释放与槽复用 | 用户已确认关闭 |
| 02 | 保存主 KV 与 Index K，alive 释放后保留 prefix | 自动化 NPU 验收通过，待用户确认关闭 |
| 03 | 命中、恢复、fill、后缀计算与两段就绪 | 尚未实现；实施依赖 02 关闭 |

本轮依据 [03 正式票](issues/03-prefix-restore-and-suffix.md) 与 [spec](spec.md)。
当前代码为 `87157b7365` 加工作区文档修改。
核对范围可以继续；本页不变更任何票的验收状态。

## B 用户最终能观察到什么

下面按 A → B → C 串行运行。prefix 从空开始，每 rank 独立保留 40 页。
统一 `page_size=128`，输入为 token IDs，使用 greedy 生成。

| 请求 | prompt | 命中 token | P 新算 token | 本次新增 prefix 页 | 累计 prefix 页 |
|---|---|---:|---:|---:|---:|
| A | 4096 token | 0 | 4096 | 32 | 32 |
| B | A 加 1024 token，共 5120 | 4096 | 1024 | 8 | 40 |
| C | 与 A 相同，共 4096 | 3968 | 128 | 0 | 40 |

命中上限为 `floor((prompt_len - 1) / page_size) × page_size`。
C 恢复 31 个完整页，最后 128 token 在第 32 页计算并采样首 token。
旧记录中的 4032 和半页接续已被统一页设计替代。

B、C 的完整输出 token IDs，各自与独立初始化的冷算参考比较。
02 的 B、C 仍是冷算保存；03 才会验证这里的非零命中长度。

## C 为什么命中后要复制两次

```flow LR
已保存的prefix -> P原生HBM: load_back
P原生HBM -> P后缀计算: 全部必要rank恢复完成
已保存的prefix -> 本请求P_alive: fill；load_back完成后启动
P后缀计算 -> 本请求P_alive: 写回新算段
本请求P_alive -> D生成: 完整ready且Index K与handoff均到齐
```

| 操作 | 复制到哪里 | 用途 |
|---|---|---|
| load_back（op2） | prefix → P 原生 HBM | P 用命中段计算后缀 |
| fill（op1） | prefix → 新请求 P alive | D 经该请求绑定读取命中段 |
| admit（op3） | P alive → prefix | 结束后保存缺失页，沿用 02 基础 |

load_back 和 fill 都包含主 KV 与 Index K。
主 KV 有 78 层，compact 576 维；恢复 HBM 时拆回 512／64 两部分。
Index K 有 21 个 producer 层、128 维，须另行恢复。

原生 allocator 返回真实 token 索引，可能不连续。
`prefix_indices` 必须有 `hit_len` 个逐 token 索引。
C 对应 3968 个索引，且不包含尚未计算的最后 128 token。

命中请求仍获得独立 P／D alive，分别扣除 64／1 页整槽额度。
按生成上限 128，B／C 的 D Index K 分别预留 41／33 页。
P 原生 HBM 仍须容纳完整 prompt，包括恢复段和新算段。

## D 最容易写错的是完成条件

**load_back 完成后，fill 与后缀计算可以独立推进。**
fill 写 `[0, hit_len)`，后缀写 `[hit_len, prompt_len)`。
是否在设备上实际重叠，必须由 NPU 测量确认。

以请求 B 为例，下面的“完成”包含全部必要 rank 的主 KV 和 Index K。

| fill 命中段 | 后缀写回 | alive 的连续 ready_tokens | D 能否开始 |
|---|---|---:|---|
| 未完成 | 已完成 1024 token | 0 | 继续等待 |
| 已完成 4096 token | 未完成 | 4096 | 继续等待 |
| 已完成 | 已完成 | 5120 | 还须等完整 Index K 与 handoff |

后缀使用独立范围跟踪器，内部计数从零开始。
不能把旧写回计数器预填成 `hit_len`，冒充命中段已经写入 alive。

P 发送由恢复段和新算段组成的完整 Index K，并交接首 token 与 metadata。
D 必须同时等到 Router ready、全部必要 rank 的 Index K 和 handoff。

P 后缀写回与原生发送完成后，可释放原生 HBM／请求行。
该释放不等待 fill，因为 fill 直接读取 prefix。
alive 整槽仍须等最后复制依赖及显式 `free_ack`，才能归还额度。

Main 在计划 load_back 和 fill 时，分别保护 prefix 来源。
每个操作只有收到真实完成 ack，才按自己的 `op_id` 解锁一次。
迟到 rank 不得造成提前 ready；重复 ack 不得重复解锁。
恢复、复制或推理失败时沿用 fail-stop，并停止新操作与 free。

## E 代码从哪里接上

以下是当前代码的阅读顺序与候选接线点，具体改动清单留给实施计划。

| 顺序 | 功能源码 | 已有基础与 03 新增职责 |
|---|---|---|
| 1 | [ascend/mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 当前固定 `hit_len=0`；增加连续 ready 匹配、恢复计划和源保护 |
| 2 | [ascend/mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | 扩展请求、复制操作和完成回执合同 |
| 3 | [ascend/mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 在有序 tick 中执行恢复与 fill，并收齐 TP rank 完成证据 |
| 4 | [npu/mempool/prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | 当前实现 admit；新增 prefix 到 alive、到原生 HBM 的复制路径 |
| 5 | [ascend/mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 接入原生分配、逐 token 映射、后缀准入与原生释放 |
| 6 | [npu/mempool/runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 当前按连续写回计数；新增独立后缀范围和真实完成证据 |

优先使用 Ascend/NPU 内的接线点。
若原生分配或调度必须增加公共 hook，实施计划须说明原因。
同时说明现有 Ascend/NPU hook 为何无法提供所需行为。

建议按“匹配与保护 → 真实恢复 → fill 与后缀 → D 就绪与释放”逐步实现。
每步补对应行为测试，并立即更新 [目标页 D](target-alignment.html#panel-D)。

## F 怎样算完成这一票

| 验证面 | 必须拿到的证据 |
|---|---|
| 内容 | 逐 rank、逐必要层、逐逻辑 token 核对 alive、HBM 两部分与 Index K |
| 原生映射 | 非连续 token 索引正确；C 的 31 页恢复与第 32 页新算正确衔接 |
| 时序 | 延迟 fill、迟到 rank、重复 ack、P native 先释放均有行为检查 |
| 推理 | B／C 真实命中；D Graph 读取新绑定；完整输出对齐各自冷算参考 |
| 保存与释放 | B 只增 8 页；C 不重复保存；整槽仅在实际 free_ack 后复用 |
| 自动化 | 每机一条命令，自动准备参考、运行 A/B/C、断言、收集与清理 |

日志与报告保存在 `/home/cryang/log/`，失败返回非零。
CPU 检查、NPU smoke、完整验收与用户确认分别记录。
03 的新复制方向和命中推理须有自己的硬件证据。

本票保持串行、单 chunk、本地 prefix、每 rank 私有副本。
共享存储、远端 prefix 和逐层流水属于后续工作。
04 再整合满缓存 E、全部拒绝和失败场景的完整验收。

本页仅核对票、规格和当前源码；未修改功能代码或执行新增 CPU／NPU 测试。
实施后的关闭流程以 [阶段验收规则](../ascend-mempool/verification.md) 为准。
