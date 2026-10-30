---
title: K1-ticket05：P alive 主 KV 共享
subtitle: 本地 05 · K1-03a · 实现交付与源码阅读顺序
template: doc
theme: blueprint
lang: zh
date: 2026-10-30
status: 实现、CPU与双轴评审完成；等待用户 NPU 验收
---

**P 主 KV 从 16 份变为 8 份，共享组成员分工写整页。**
D 主 KV、P Index K、prefix 主 KV 和 prefix Index K 各保留 16 份。
基点为 `4c0e79b55046ba69a1559eaab39d061ce3eec8ed`。
本票保持 open，NPU 尚未运行。

## A 正常执行路径

每组 BM 会话包含两个 P 成员和两个 D 成员。
P owner 分配共享主 KV 和自己的附属区域。
另一 P 成员只分配自己的 Index K、prefix 和配对探针。
每个 D 成员分配自己的主 KV。

```flow LR
P0 -> 共享主KV: 写前半页
P1 -> 共享主KV: 写后半页
共享主KV -> D0: 读完整副本
共享主KV -> D1: 读完整副本
```

`prefill_groups` 指定分组，首成员为 owner。
`prefill_write_assignment` 控制连续分块或轮转。
每次 forward 的主 KV 计数与 Index K 计数分别核对。
因此一个 rank 可以没有后缀主 KV 写入，仍须完成其 Index K 写入。

prefix fill 只写本成员负责的主 KV 页，并恢复全部私有 Index K。
admit 从共享主 KV 和本 rank 的 Index K 保存到私有 prefix。
SR 合并同组页位图，再取各副本与 Index K 的共同完成进度。

Main 先收齐 D 完成及 drain，再发 P free。
SR 等齐写者清理并完成全 rank 回执后，Main 才归还额度。
槽和额度继续使用逻辑容量；物理份数由注册和去重后的页描述符表达。

## B Ascend/NPU 功能源码

按配置、存储、执行、协调的顺序阅读：

| 顺序 | 文件 | 用途 |
|---|---|---|
| 1 | [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | P 共享开关、分组、写策略与两侧副本数 |
| 2 | [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | 会话成员、每个成员的贡献量、区域 owner 与 NUMA |
| 3 | [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 实际 BM 分配、映射、私有探针、区域视图与注册 |
| 4 | [page_write.py](../../python/sglang/srt/hardware_backend/npu/mempool/page_write.py) | 整页写分工，供计算写回、fill 和 SR 使用 |
| 5 | [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 按页写主 KV、完整写 Index K，保留真实完成行数 |
| 6 | [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | 分工 fill、共享源 admit、独立完成事件 |
| 7 | [mempool_tick.py](../../python/sglang/srt/disaggregation/ascend/mempool_tick.py) | 完成事实携带实际主 KV 写行数 |
| 8 | [mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 从 runtime 或脱离原生行后的 receipt 提取写入事实 |
| 9 | [mempool_accounting.py](../../python/sglang/srt/disaggregation/ascend/mempool_accounting.py) | 按副本合并页完成，并保留各 rank 的 Index K 门控 |
| 10 | [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 共享描述符、双读者记账、就绪和释放汇总 |
| 11 | [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 接线配置、区域注册、NUMA 证据和验收探针 |
| 12 | [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 接受 P/D 不同份数，去重不透明共享页描述符 |
| 13 | [shared_probe.py](../../python/sglang/srt/hardware_backend/npu/mempool/shared_probe.py) | 验收开关下在请求接入前核对跨写者 NPU 读写 |

## C 通用主线与目录例外

本票没有通用主线功能源码改动。
SGLang 默认退出路径保持原有实现。

目录外改动位于 `ascend-mempool-test/`，用于独立测试与运行编排。
这些代码运行在测试进程，不属于 worker 的 Ascend/NPU hook。
功能源码清单与脚本、测试、说明文档分别列出。

## D 脚本、测试与运行说明

| 文件 | 用途 |
|---|---|
| [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py) | 新增 p-sharing-acceptance，复用两轮服务、清理与报告 |
| [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py) | 复用原有 ABCE、故障、重启及释放检查 |
| [verify_mempool_shared.py](../../ascend-mempool-test/scripts/verify_mempool_shared.py) | 核对共享区域、贡献量、跨写者哨兵与写入计数 |
| [K1_TICKET05.md](../../ascend-mempool-test/K1_TICKET05.md) | 环境、逐机命令、覆盖边界和日志回传清单 |

CPU 测试按公开接口覆盖池会话、runtime/prefix、SR/Main 和验收入口。
组合测试贯穿共享写入、私有 Index K、admit 与 fill。
证据测试拒绝把附属区域误报为共享，也拒绝缺少跨写者读取。

502项CPU覆盖最终全部通过；25文件静态检查与15模块strict mypy通过。
两轴评审发现的问题已修正，复审均为0项。
详细测试记录见 [本机验证](verification/ticket05-cpu-20261030/README.md)。
测试文件清单与真实执行次数记录在该目录。

## E 验收边界

默认 BM 本机贡献量为 P 120 GiB、D 16 GiB。
P owner 每组 13 GiB，另一成员的私有区域每组 2 GiB。
实际数字在 BM join 时核对，再按 NUMA 汇总。

物理 NUMA 驻留独立记录，容器权限不足时保持 unverified。
自运行入口标为精简功能验收，沿用独立答案和容量 CPU 重放。
完整性能复测按约定留在测试分支。

只有用户确认本票 NPU 结果后才能关闭 05。
本票关闭后，08 还需等待 07 自身验收。
