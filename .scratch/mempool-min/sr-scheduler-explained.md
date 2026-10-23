---
title: 用一个请求看懂 SR 与 Scheduler
subtitle: 协调者汇总全组，分片执行本地操作，Scheduler 安排计算
template: doc
theme: blueprint
lang: zh
status: SR 是 ticket01 的拟定实现；Scheduler 已存在
---

SR 协调者和 SR 分片共同组成一侧的 Sub Router。
协调者负责本侧统一分配、下发命令和汇总完成。
分片在各 rank 内执行本地存储操作。
Scheduler 是每个 rank 内持续运行的推理调度循环。

本文的私有副本和 slot 示例限定在 syncfile 的 sparse 阶段 0。
K1 会让两个 rank 共用一份 alive 主 KV，SR 分片仍各 rank 一个。
后续目标及阶段对应见[目标对齐核对](target-alignment.md)。

## A 它们在哪里

以 P 机的 TP16 为例。一台机器有 16 个参与推理的 rank。
每个 rank 都有 Scheduler、SR 分片及模型执行模块。
rank 0 还放置 SR 协调者。

```tree
P 机：一个 TP16 推理实例
  rank 0 进程
    Scheduler 0
      SR_P 协调者：与 Main Router 通信，协调全组
      SR_P 分片 0：处理 rank 0 自己的存储
      ModelWorker 0：执行本 rank 的模型计算
  rank 1 进程
    Scheduler 1
      SR_P 分片 1：处理 rank 1 自己的存储
      ModelWorker 1：执行本 rank 的模型计算
  rank 2 至 rank 15
    各自的 Scheduler、SR_P 分片、ModelWorker
```

图表示模块归属，不表示这些对象是新增进程。
D 机使用相同组织方式，换成 SR_D。

**“分片”在这里是 SR 按 rank 拆开的执行部分。**
ticket01 使用各 rank 私有的 compact 主 KV 副本。
对于 4096-token prompt，每个 rank 保存自己那份完整逻辑主 KV。
同一逻辑位置在不同 rank 中对应各自的存储和设备映射。

## B 例子：请求 A 需要保存 4096-token prompt

假设所有 P 槽都空闲，协调者为 A 选择最小空闲槽 0。
“slot 0”在这里表示各 rank 私有池中编号相同的槽。
全部 P ranks 必须确认同一请求与分配结果。

这里的 slot 是 alive 临时后端的分配单位。
S_P = 8192 时，一个槽占 64 个 B-token 逻辑页额度。
4096-token 请求只写入前 32 页，槽尾仍被占用。
prefix 在阶段 0 已按页分配；alive 的真正页分配器安排在阶段 3。

| 时刻 | SR_P 协调者 | 每个 SR_P 分片 |
|---|---|---|
| 收到 alloc(A) | 选择槽 0，在有序 tick 中下发 | 校验身份，在本 rank 的池中占用槽 0 |
| 分配结束 | 收齐 16 个成功结果，回 alloc_ack | 返回本地分配结果和描述符 |
| P 开始计算 | 保留请求与分配记录 | 配合本 rank 的 runtime 跟踪写回完成 |
| rank 7 仍有写回未完成 | 继续等待 | 其他分片报告完成；分片 7 报告未完成 |
| 16 个 rank 全部完成 | 向 Main Router 报 ready_tokens=4096 | 本地设备事件证明写回已完成 |
| 最后收到 free(A) | 下发释放，收齐结果后回 free_ack | 检查本地依赖，归还自己的槽 0 |

协调者对 Main Router 报告的是“本侧整体完成”。
单个分片完成，只能证明本 rank 的那一份已完成。
rank 0 的本地工作也由分片 0 完成，并计入这 16 份结果。

```sequence
participants: Main, 协调者, 分片0至6, 分片7, 分片8至15
Main -> 协调者: 为 A 分配 P alive
协调者 -> 分片0至6: 在各自池占用槽 0
协调者 -> 分片7: 在本地池占用槽 0
协调者 -> 分片8至15: 在各自池占用槽 0
note 协调者, 分片8至15: 收齐实际分配结果后，协调者向 Main 回 alloc_ack
note 协调者, 分片8至15: dispatch 后，各 Scheduler 驱动共同的 prefill
分片0至6 --> 协调者: 主 KV 写回完成
分片8至15 --> 协调者: 主 KV 写回完成
note 协调者: rank 7 未完成，继续等待
分片7 --> 协调者: 主 KV 写回完成
协调者 --> Main: ready_tokens = 4096
```

图中分组仅用于展示，实际通过 TP 组通信汇总。
单 chunk 的本例在全部完成后报告 4096。
后续若支持分段进度，也只能报告所有必要分片均完成的连续范围。

## C Scheduler 每一轮做什么

Scheduler 决定本轮运行哪个请求、运行哪种 forward，以及何时处理结果。
它调用 ModelWorker 执行实际模型计算，并管理原生请求的推进。

| 工作 | 当前请求 A 中的含义 |
|---|---|
| 接收输入 | 接收原生 Req，推进控制命令和完成事件 |
| 检查能否运行 | 检查资源、绑定，以及当前阶段要求的就绪条件 |
| 组织 batch | 将可以运行的请求放进本轮计算；本 ticket 最多一个真实请求 |
| 调用模型 | P 执行 prompt prefill；D 执行下一步 decode |
| 处理结果 | 更新输出 token、长度、EOS 和停止状态 |
| 推进清理 | 触发原生资源清理，并让 SR 观察完成与释放条件 |

调度循环可以概括为以下伪代码：

```text
while 服务运行:
    接收请求和控制消息
    推进本轮 SR 操作与完成检查
    处理原生 bootstrap / transfer 队列
    选择已经满足条件的计算 batch
    调用 ModelWorker 执行 forward
    处理可用结果，更新请求状态
```

实际 P/D 循环有各自的队列，overlap 模式还会交错处理前一轮结果。
以上只表达职责，不是要求按此重写现有循环。
一次 tick 是一次调度循环迭代；它可以只处理控制消息。
它不保证每轮都生成 token，也不保证设备在返回时已完成。

**每个 rank 都有 Scheduler。**
TP ranks 处理同一组请求，并保持兼容的计算与通信顺序。
各 rank 执行模型中分配给自己的部分，合起来完成一次 forward。

普通 decode 中，P 的首 token 通过 handoff 交给 D。
D 各 Scheduler 满足门控后，让各 ModelWorker 处理这个 token。
这次 forward 产生下一个 token，随后重复执行，直到停止条件满足。

## D Main Router、SR 和 Scheduler 如何配合

| 角色 | 主要回答的问题 | A 的例子 |
|---|---|---|
| Main Router | 这个请求能否接收？跨 P/D 应推进到哪一步？ | 预留四类资源，通知 D ready，最后下发 free |
| SR 协调者 | 本侧应该使用哪些存储？所有 rank 完成了吗？ | 选槽 0；等待全部 16 份写回 |
| SR 分片 | 本 rank 的存储在哪里？本地操作完成了吗？ | 解析本地描述符，观察事件，归还本地租约 |
| Scheduler | 本轮哪些计算可以运行？结果和请求如何推进？ | 条件满足后启动 prefill / decode，处理 EOS |
| ModelWorker / runtime | 如何执行模型与设备读写？ | 执行 forward、mempool 写回与稀疏读取 |

将 SR 放入 Scheduler 的有序 tick，有两个直接作用。
计算启动前可以检查存储绑定与就绪状态。
各 rank 可以按一致顺序参加 SR 和模型所需的组内通信。

网络接收线程只把消息排队。
Scheduler 在约定位置推进 SR，避免不同 rank 在同一时刻等待不同的通信。
D 完成生成后，Scheduler 推进 drain 和原生清理。
SR 汇总 finished，Main 下发 free，再由各分片实际归还 alive。

SR 的上述职责是 [ticket01 方案](ticket-01-implementation-plan.md) 中拟新增的部分。
现有 [Scheduler](../../python/sglang/srt/managers/scheduler.py) 已负责接收、batch 和模型调用。
其 ingest_requests 已调用 mempool_service.advance，准备沿用这一接入点。
实际 P/D 主循环见 [prefill.py](../../python/sglang/srt/disaggregation/prefill.py) 与
[decode.py](../../python/sglang/srt/disaggregation/decode.py)。
