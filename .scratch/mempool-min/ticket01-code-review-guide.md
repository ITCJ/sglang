---
title: ticket01 功能代码与 review 顺序
subtitle: Main / SR 冷请求生命周期、原生资源预留与 warmup
template: doc
theme: blueprint
date: 2026-10-26
baseline: 3883624810 → 7275f29a05
scripts: ascend-sglang-script 42a1511
---

ticket01 建立 Main → SR → 真实 P/D 冷请求 → 显式释放的完整路径。
最终净差异包含 16 个 SGLang 运行时文件。
其中 12 个位于 Ascend/NPU，4 个位于共享主线。
独立脚本仓库另有 3 个 demo 文件。

以下清单不包含测试、验收 runner、README 和其他说明文件。
统计基于最终净差异，包含原生 warmup 修复。
HTTP 重试补丁已完全撤回，http_server.py 没有净差异。
旧 ticket02 已撤回，prefix 保存和命中恢复尚未实现。

## A 本票实现了哪条路径

```sequence num
Client -> Main: 提交对齐 token IDs
Main -> SR_D: alloc，预留 alive、HBM 页和请求行
SR_D --> Main: 全部 rank 完成分配
Main -> SR_P: alloc，取得 P alive
Main -> SR_D: dispatch，确认绑定
Main -> SR_P: dispatch，确认绑定
Main -> P/D: 并行发送原生 HTTP 请求
SR_P --> Main: prompt 写回完成，ready_tokens
Main -> SR_D: ready，等待 Index K 和 handoff 就绪
SR_D --> Main: decode 完成、drain、native 清理后 finished
Main -> SR_P: free
Main -> SR_D: free
SR_P --> Main: 实际释放后的 free_ack
SR_D --> Main: 实际释放后的 free_ack
Main --> Client: 输出结果，允许下一请求
```

Main 保存配额账本、页目录、操作编号和请求阶段。
各侧 rank 0 协调 SR，每个 TP worker 持有一个分片。
分片执行实际分配，返回不透明的页描述符。
Main 不保存设备地址，也不替 worker 选择物理页号。

## B 存储、同步与范围

| 对象 | 当前行为 |
|---|---|
| P prompt 主 KV | 保存在 P DRAM alive，D 完成前保持有效 |
| D 生成主 KV | 保存在 D DRAM alive；位置扣除 prompt 长度 |
| Index K / handoff | 沿用原生 P/D 传输；D 提前预留 Index K HBM 页 |
| DRAM → HBM | 沿用两次 UniDexCopy，分别读取 P 和 D 来源；本票未修改复制 kernel |
| D 原生资源 | HTTP 请求到达前，预留一行及完整 prompt + generation 页预算 |
| 完成与归还 | finished 确认 D drain/native 清理；alive 配额在各侧 free_ack 后归还 |
| 错误和超时 | fail-stop，保留未确认安全的资源；不自动当作完成释放 |
| token 页 | DRAM、HBM、allocator、KV pool、Index K 传输统一为 128 |
| 执行范围 | GLM-5.2，P/D 各 TP16；P eager，D Graph BS=16，单真实请求 |
| 当前容量 | SP=8192、SD=128；只接受对齐 token IDs、greedy、非流式 |
| prefix / 共享 | prefix 未实现；本票沿用每 rank 私有副本 |

## C Ascend / NPU 文件阅读顺序

所有路径均位于 python/sglang/srt 下。
先读协议和 Main，再顺着传输、SR、原生预留进入服务与设备侧。

| 顺序 | 文件 | 净改动与 review 重点 |
|---|---|---|
| 1 | [ascend/mempool_router_protocol.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py:24) | 新增 RouterConfig、运行环境键、原生消息编码桥接、模型配置摘要和页哈希链。核对 page128、容量与上下文余量。 |
| 2 | [ascend/mempool_router.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_router.py:46) | 新增 MainRouter。注册两侧 16 rank，维护四类 quota、目录、active 和 operations；校验回执后推进 ready、finished、free。 |
| 3 | [ascend/mempool_router_transport.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_router_transport.py:53) | 新增 HTTP/ZMQ 适配和 attach_subrouter。校验实际页大小、容量和模型；提供 generate/status/ready/health；向 P/D 发原生请求。 |
| 4 | [ascend/mempool_subrouter.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_subrouter.py:25) | 新增 RouterShard 和 MempoolSubrouter。选最小空闲槽，核对 dispatch，执行分配/绑定/释放；有序 tick 以两次 all-gather 汇总事实和结果。 |
| 5 | [ascend/mempool_native_reservation.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_native_reservation.py:16) | 新增 NativeReservation / NativeReservations。按 room 保存 row、indices、prompt_tokens、limit、req；一次预留，逐 token 取用，drain 后归还全部页。 |
| 6 | [ascend/mempool_control.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_control.py:531) | 增加 router_owned 和 router_acquire_prefill/bind/ready/free。沿用已有 lease、generation 和 session 校验；Router 模式由显式 free 归还槽。 |
| 7 | [ascend/mempool_tick.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_tick.py:314) | Router 模式保留原生启动和心跳推进，关闭旧请求自动 acquire/ready/release，避免两套控制逻辑修改同一槽。 |
| 8 | [ascend/mempool_service.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/ascend/mempool_service.py:270) | 接入 SR，fake 请求先绕过真实请求跟踪；每步记录真实请求数量；推进 SR、调用 native 释放，并将真实请求异常转为 fail-stop。 |
| 9 | [npu/mempool/config.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/hardware_backend/npu/mempool/config.py:17) | MempoolConfig 增加 page_size，从 server args 传入，并要求两侧容量按 native 页对齐。 |
| 10 | [npu/mempool/manager.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/hardware_backend/npu/mempool/manager.py:176) | 记录创建 BM pool 时请求的 numa_node，供 SR 注册。该字段不证明物理页实际落点。 |
| 11 | [npu/mempool/runtime.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:542) | 增加 P/D BM 来源计数与完成快照。Graph 内采集，完成事件后读取，形成请求完成证据。计数是被选中的行，不是唯一页数。 |
| 12 | [npu/graph_runner/npu_cudagraph_backend.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/hardware_backend/npu/graph_runner/npu_cudagraph_backend.py:140) | capture 日志增加 batch_rows。用于识别 Graph 的批宽；本文件仅修改日志，没有改变 capture/replay 算法。 |

重点检查 Main 的 submit / _receive / _complete。
再检查 SR 的 execute / advance 和 NativeReservations 的 reserve / release。
前者负责何时推进，后者负责实际资源归属。

## D 四处共享主线改动为何必要

这四处累计新增 29 行、删除 4 行。
预留策略和状态都在 Ascend 模块；主线提供调用点。
原生检查、分配和请求字段修改发生在这些函数内部。
仅在 Ascend 模块新增方法，不能改变主线调用顺序。

| 顺序与文件 | 实际改动 | 必要性及调用点为何在主线 |
|---|---|---|
| 13 [disaggregation/prefill.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:349) | 把已有 service.track(req) 从 add 尾部移到 create_sender 中、_process_req 前。 | _process_req 会把 max_new_tokens 改成 1。SR 必须在修改前核对 Main 原始生成预算，否则合法 dispatch 会不匹配。原调用时机由此主线函数决定。 |
| 14 [disaggregation/decode.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:195) | 增加 alloc_rows/free_rows；已预留请求跳过重复的空闲容量检查；pre_alloc 通过 attach 接入已有 row/indices。 | SR 分配时还没有原生 Req。请求到达后需要复用预留，避免二次分配或因已占用资源被误拒绝。原生行池、入队检查和 pre_alloc 入口都在此文件。 |
| 15 [managers/schedule_batch.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/managers/schedule_batch.py:3229) | check_decode_mem 调用 reservations.can_decode。 | 资源已提前扣除，全局 free pages 不代表本请求是否还能 decode。该容量判断在进入实际分配前执行，需要此处检查预留剩余量。 |
| 16 [mem_cache/allocation.py](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/allocation.py:587) | alloc_for_decode 返回 reservations.decode 的下一段预留 indices。 | 实际每步 out_cache_loc 在此产生。这里必须消费已有预留，避免再次从全局 allocator 分配。具体索引和长度更新仍由 Ascend 类实现。 |

非 Router 路径继续执行原逻辑。
Router 模式下，applies_to 只允许明确的 fake warmup 走原生分配路径。
真实请求缺少预留仍报错，fake 与真实请求混批也会被拒绝。

## E Warmup 与独立启动入口

P/D 各自执行原生 fake warmup，请求资源由本机原生分配和释放。
fake 请求不进入 Main/SR 的真实请求账本，也不建立真实 alive 绑定。
本次适配保留原生 HTTP warmup 时序；HTTP 重试改动已撤回。

脚本仓库的提交为 42a1511，三个文件位于 pd-disaggregation 下。
这些文件放在用户指定的启动脚本目录，不属于 SGLang 主线修改。

| 文件 | 功能 |
|---|---|
| [glm52mempool-demo.json](/Users/hibikid/Documents/sparsekv-ascend-sglang/ascend-sglang-script/pd-disaggregation/glm52mempool-demo.json) | 共享 31/32、模型、NUMA、端口、页大小和容量配置。 |
| [glm52mempool-demo.sh](/Users/hibikid/Documents/sparsekv-ascend-sglang/ascend-sglang-script/pd-disaggregation/glm52mempool-demo.sh) | 按 LOCAL_HOST1 启动 P/D；复用现有服务启动环境，P eager、D Graph BS=16，原生 warmup 开启。 |
| [router-mempool-demo.sh](/Users/hibikid/Documents/sparsekv-ascend-sglang/ascend-sglang-script/pd-disaggregation/router-mempool-demo.sh) | 独立启动 Main Router，使用同一 config / run-id，日志保存到 /home/cryang/log/。 |

## F 已有验证与本次核对

7275f29a05 的 31/32 自动化 NPU 验收已通过。
覆盖冷请求完整输出一致性、D Graph、BM 来源、释放复用和超时 fail-stop。
手动自然语言请求也已通过：128-token 输入，48-token 输出，答案正确。

本次按最终 Git diff 核对全部功能文件与四处主线调用点。
没有修改功能代码，也没有重新执行硬件测试。
prefix 与物理 NUMA 页落点不在这些通过结论内。
