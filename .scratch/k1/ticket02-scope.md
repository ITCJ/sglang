---
title: K1-ticket02 要实现什么
subtitle: K1-02b：共享池适配器接入（share=0）与记账骨架
template: sheet
theme: blueprint
cols: 2
lang: zh
date: 2026-10-29
---

## A 本票的交付结果 {span=2}

```callout info 实现前的范围讨论快照
本页记录基点 993ae38ed6 上的范围讨论，表中的“当前”均指实现前。
代码现已交付并通过 CPU 检查；最新状态、精简验收与源码阅读顺序见交付记录。
```

[K1-ticket02 交付记录](delivery-ticket02.md)。NPU 验收仍待用户运行。

```callout info 先在真实服务中验证适配器接入
服务统一通过共享池适配器创建和访问池。
本票使用 share=0，每个 rank 仍独占自己的副本。
就绪与释放改为按副本记账，为后续共享提供接口。
```

| 项目 | 当前依据 |
|---|---|
| 票号 | 本地票 02，对应 K1-02b |
| 状态 | ready-for-agent / open；尚未实施 |
| 前置条件 | 本票没有 blocker；票 01 也已获用户验收 |
| 本票副本数 | P/D alive 主 KV 各 16；其余三类各 16 |
| 后续工作 | 05 做 P 主 KV 共享；06 做 D 主 KV 共享；07 做 P Index K 单份 |

依据：[票 02](issues/02-k1-02b-shared-pool-adapter-share0.md)、[执行文本第 6 节](k1-tickets.md#6-票-02k1-02b-共享池适配器接入share0与记账骨架)。

## B 池与会话怎样接入

```flow LR
服务启动 -> 池适配器: 创建与访问
池适配器 -> BM会话: 显式参与者
BM会话 -> P私有池: P_i贡献
BM会话 -> D私有池: D_i贡献
```

每侧副本的成员列表都是 `[i]`，owner 是该侧 rank i。
一个跨 P/D 的 BM 会话仍包含 P_i 和 D_i 两个参与者。
“成员数 1”指每侧副本成员数，不是 BM 会话总人数。

将两参与者专用接口换成能表达多成员的接口。
接入端口、BM 初始化、池映射、Graph 访问和关闭路径。
请求结束后复用槽位；服务关闭前先排空设备访问。

现有服务入口仍调用 `initialize_rank_pair()`。
实验中的 `SharingPool` 已能区分参与者编号与 P/D 逻辑视图。
本票把该能力接到服务生命周期中。

## C ready_tokens 改为覆盖位图

协调者为每份副本记录“哪些页已实际写完”。
它从第 0 页起计算连续完成范围，再汇总必要副本与 Index K。

```flow
命中段fill完成 -> 副本覆盖位图: 记录实际完成页
新算段写回完成 -> 副本覆盖位图: 记录实际完成页
副本覆盖位图 -> 公共连续范围: 汇总必要副本
公共连续范围 -> ready_tokens: 同时满足Index_K完成
```

命中长度本身不能证明 fill 已完成。
新算段写完，也不能跳过未完成的命中段。
保留现有写入连续性检查和首 token 的三条件门控。

本票仍是单写者。
验收时，让位图与原计数器消费同一轮完成事件。
两种算法的 ready_tokens 序列必须逐事件相同。

## D free_ack 改为读者计数

协调者为每份副本记录尚未停止访问的读者数。
本票初值为 1；对应读者完成 drain 后减为 0。

```flow LR
读者停止并完成drain -> 读者数归零: 确认完成
读者数归零 -> 既有释放流程: 满足其他释放条件
既有释放流程 -> free_ack: 实际清理完成
```

计数归零是释放条件之一。
保留 native 清理、prefix 保存及 source 释放约束。
对 Main 发出 free_ack 的逻辑时点应与阶段 0 一致。

后续 05 / 06 会把对应计数扩展到两个读者。
本票先验证一个读者时的行为等价。

## E 注册需要补齐什么

| 信息 | 当前代码 | 本票结果 |
|---|---|---|
| 四类副本数 | 已有 replicas() 上报 | 保持与实际池布局一致 |
| owner | 显式 NUMA 映射路径已有 owner_tp_rank | 每份副本明确 owner |
| 成员关系 | 尚未形成每副本成员列表 | share=0 时 members=[i] |
| 会话与映射 | 服务按 rank-pair 创建 | 能回查参与者、会话与 NUMA |

四类分别是 alive 主 KV、P alive Index K、prefix 主 KV、prefix Index K。
P/D alive 主 KV 的副本数分别核对。
Main 的请求消息、页表语义与账本规则保持现有约定。

## F 复用哪些实现，从哪里接入 {span=2}

NUMA 分配和共享池组件已有实现。
下列原始提交均在当前 HEAD 的祖先链中，可直接沿用当前代码。

| 原始提交 | 已有能力 | 本票如何复用 |
|---|---|---|
| c89416cd4c / 5067e7cb21 | TP rank 绑定 NUMA；显式节点列表 | 继续使用建池时的 NUMA flags |
| d328af12bd | 分组、owner、participant、贡献量、probe、端口规划 | 将现有规划接到服务会话适配器 |
| 787d10a287 | 节点列表按 rank 连续均分 | 保留分配函数与实际建池路径 |
| 2a2007e82f / bb53ac0f33 | 实验版 SharingPool，覆盖 TP2 / TP4 | 复用建池与映射机制，接入服务生命周期 |
| 4d305b95d3 / 3883624810 | 8 组共享 96 GiB 启动入口与硬件结果 | 保留组件证据；服务仍需本票独立验收 |
| 993ae38ed6 | 显式映射用于真实建池；注册核对 applied；驻留证据 | 沿用已验收的 K1-01 功能 |

`db13acd16e` 是 codereview 分支的工程整理提交。
提交说明记录其来源为 `d2c999b4fa..3883624810`。
已核对四个核心文件，与 `787d10a287` 版本逐字节一致。
当前分支已有原始提交，无需再次 cherry-pick 该整理提交。

旧规划文档曾写“显式映射在生产启动时被拒绝”。
这是当时的限制，已由 `993ae38ed6` 解除。
生产共享启动仍待后续票接入。

下表是实现前的阅读顺序与接入点，尚未修改功能代码。
主要改动可落在现有 npu / ascend 目录内。

| 顺序 | 功能源码 | 对应职责 |
|---|---|---|
| 1 | [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | 复用 NUMA 分配、成员分组、owner 和会话参数规划 |
| 2 | [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | BM 会话、建池、映射、端口与关闭；目前固定两参与者 |
| 3 | [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 服务建池入口、Graph 所用视图、写入连续性和完成事实 |
| 4 | [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 汇总 rank 完成事实；承接覆盖位图与读者计数 |
| 5 | [mempool_control.py](../../python/sglang/srt/disaggregation/ascend/mempool_control.py) | 保留 KV_READY、drain 和请求所有权约束 |
| 6 | [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 将副本数、owner、成员与映射信息放进注册快照 |

代码核对基于 `993ae38ed6` 及当前工作区。
本页只整理范围，未运行 CPU 或 NPU 测试。

## G 怎么验收，接下来走什么流程 {span=2}

| 验收面 | 需要证明的结果 |
|---|---|
| 模型与功能 | A/B/C/E、V1–V10 全过；包括 prefix 保存、命中、释放复用 |
| 就绪等价 | 同轮位图与原计数器的 ready_tokens 序列逐事件相同 |
| 释放等价 | 读者数从 1 归零；free_ack 保持原有顺序和清理条件 |
| Graph | 真实模型的 capture/replay 能通过适配器访问池 |
| 关闭与重启 | 正常退出、fail-stop 各验证一次；会话、共享段、端口释放后可重启 |
| 注册证据 | 四类副本数与每份副本的 owner、成员关系齐全 |

交付自运行验收入口；双机可各启动一次命令。
脚本自动等待服务、发请求、核对结果、归档并清理自身进程。
日志与报告存入 `/home/cryang/log/`，失败返回非零。
本票仍需用户执行并确认 NPU 验收后才能关闭。

按 2026-10-29 的范围更新，性能工具留在测试分支。
旧票的基线对照与启动耗时等性能项在那里另行安排。
工程分支的功能验收不代表性能已验收。

按 ask-matt，这张票已可交给 `/implement`。
实现流程为 TDD 行为切片、Standards/Spec 审查、用户 NPU 验收。
票 02 验收后，05 / 06 / 07 的现有前置依赖均已满足。
每一步实现时须同步 target-alignment 的 D 节。

来源：[最新范围约定](spec.md)、[票 01 验收](verification/k1-ticket01-002-20261029/acceptance.md)、[阶段验收流程](../ascend-mempool/verification.md)。
