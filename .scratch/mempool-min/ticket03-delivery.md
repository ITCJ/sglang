---
title: Ticket03：命中恢复与后缀计算
subtitle: 功能代码阅读顺序、所有权与双机自运行验收
template: doc
theme: blueprint
lang: zh
date: 2026-10-26
status: CPU 453 项通过；NPU 首轮 C 输出不一致，正在诊断
---

**ticket03 已实现 B 命中 4096、C 命中 3968，只计算各自后缀。**
源为 A 真实保存的 prefix，仍是 P 每个 TP rank 一份。
主 KV 与 Index K 均参与 load_back 和 fill。
本轮基线为 `87157b7365`，不包含用户先前未提交的其他票文档。

用户已明确授权先实施 03，02 的关闭确认仍单独保留。
03 保持 open，NPU 首轮验收失败，正在诊断；04 未解锁。
后续硬件修正另行提交。

**2026-10-26 运行反馈：** `0308cd0814`、`ticket03-001` 已在 31/32 执行。
target 的 C 完整输出与冷算参考不一致，P 退出，D 收到 peer failed。
最终内容/生命周期汇总和 timeout 验收未完成。
后续回传确认 A/B 输出相等，C 从 output[15] 不同；冷算 A/C 完全相等。

离线 reuse 内容检查通过，16 rank 的 A/C alive 仅第 31 页不同。
该页为重算尾页，尚未区分元数据错误与计算数值差异，保留严格比较。
[失败记录](verification/ticket03-001-20261026/failure-record.json)保留观察与未知项。

**临时诊断重测：** 两机更新代码后，使用原命令增加 --diagnose-reuse，
run-id 改为 ticket03-002。仍由用户在 31 执行 prefill、32 执行 decode。
即使 C 再次输出不一致，P 终端也会打印 REUSE_DIAGNOSTIC 和 16 条 REUSE_RANK。

复制这些行与错误即可；完整 JSON 保存在 prefill/target/reuse-diagnostic.json。
这轮用于定位根因，不能据此关闭 ticket03 或确认数值误差可接受。

诊断交付最终 CPU 459/459 通过，5 模块 strict mypy、8 文件静态检查通过。
Standards/Spec 最终均为 0 项未解决；Spec 中摘要关联问题已由六个反例验证修正。
NPU 尚未运行本轮探针，详见[检查记录](verification/ticket03-001-20261026/probe-checks.json)。

本轮新增/修改的功能源码阅读顺序如下，不含测试和说明文件：

| 顺序 | 功能文件 | 阅读目的 |
|---|---|---|
| 1 | [npu/mempool/debug_reuse.py](../../python/sglang/srt/hardware_backend/npu/mempool/debug_reuse.py) | 临时探针：核对真实元数据，只比较相同输入，保留首个冷算的一页 CPU 副本 |
| 2 | [npu/mempool/prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | 默认关闭；在完整 alive 内容校验后统计逐层尾页差异 |
| 3 | [ascend/mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 将已分配请求的输入身份和 hit_len 传给探针 |
| 4 | [npu/attention/ascend_backend.py](../../python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py) | 在 P 的 sparse attention 调用前观察实际长度与页表 |

验收入口和离线摘要脚本需要在服务外协调开关、收集退出日志，故位于既有测试目录。
探针会增加同步和 CPU 检查开销，不用于性能结论；确认根因并验收修复后删除。

## A 功能代码阅读顺序

服务功能修改全部位于 Ascend/NPU 目录。
下表只列功能源码，测试与说明文件另列。

| 顺序 | 功能文件 | 阅读目的 |
|---|---|---|
| 1 | [ascend/mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | match_prefix 开关，默认关闭，要求 prefix 区域已开启 |
| 2 | [ascend/mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 连续 ready 前缀匹配、两种读操作的独立保护、ack 与启动顺序 |
| 3 | [npu/memory_pool_npu.py](../../python/sglang/srt/hardware_backend/npu/memory_pool_npu.py) | 按实际 flat/NZ 布局写入和读取 MLA 的 512/64 两部分 |
| 4 | [npu/mempool/prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | prefix 到原生 HBM/alive 的复制、设备事件、内容与索引证据 |
| 5 | [ascend/mempool_prefill_restore.py](../../python/sglang/srt/disaggregation/ascend/mempool_prefill_restore.py) | 原生 allocator 分配命中段；通过 ChunkCache 将索引交给正常 extend 和清理 |
| 6 | [npu/mempool/runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 独立 suffix 范围、从零计数与设备完成；拒绝错位写入 |
| 7 | [ascend/mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 有序 tick 执行复制、全 TP 完成汇总和 ready 门控 |
| 8 | [ascend/mempool_service.py](../../python/sglang/srt/disaggregation/ascend/mempool_service.py) | 合并 fill/suffix；native 释放后仍保留范围收据 |
| 9 | [ascend/mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 启动安装真实原生适配器并注册匹配配置 |

验收脚本位于既有 `ascend-mempool-test/scripts/`：
`run_mempool_min.py` 协调两机服务与参考组，`verify_mempool_reuse.py` 校验 03 证据。
既有 `verify_mempool_min.py` 和 `verify_mempool_prefix.py` 复用资源与保存检查。
这些是离线测试入口，不能由服务内 Ascend hook 代替双机启动、进程清理与报告收集。

## B 数据流、所有权和同步

```sequence num
Main -> SR_P: alloc/dispatch，独立 P/D alive 与完整额度
Main -> Main: 保护 prefix 来源，分配 load_back op_id
SR_P -> P_native: 实际 allocator 索引，恢复主 KV 与 Index K
P_native --> Main: TP16 设备完成后的 load_back_ack
Main -> Main: 先保护 fill，再解除 load_back 保护
Main -> SR_P: fill；同时允许启动 P 后缀计算
SR_P -> P_alive: fill 写 [0,hit_len)
P_native -> P_alive: suffix 写 [hit_len,prompt_len)
P_native -> D_native: 完整 Index K、首 token 与 metadata
P_native -> SR_P: 后缀写回和发送完成，释放原生页与请求行
SR_P --> Main: 两段连续完成后发布 ready_tokens
Main -> SR_D: ready；D 同时等待 Index K 与 handoff
SR_D --> Main: Graph 生成结束、drain 与 native 清理完成
Main -> SR_P: 只 admit 缺失页，等待真实 ack 后 publish
Main -> SR_P: 显式 free；实际归还后 free_ack
Main -> SR_D: 显式 free；实际归还后 free_ack
```

load_back、fill 与 admit 各有独立 op_id。
load_back 完成前不启动 P；fill 未完成不能发布完整 ready。
两段可以独立推进，实际是否重叠尚待 NPU 测量。
P native 释放只等待后缀写回与原生发送，不等待 fill。
fill 的来源是被保护的 prefix，目标是新请求 alive。

恢复页归原生请求所有：cache_protected_len=0，正常清理归还整行。
C 恢复 31 个完整页，prefix_indices 长度严格为 3968。
最后 128 token 通过原生 extend 分配新页，索引不会冒充页号。
P alive 每次仍占 64 页整槽，D alive 每次占 1 页整槽。
D Index K 按完整 prompt 和 128-token 生成预算预留，B/C 分别为 41/33 页。
失败进入 fail-stop，保留资源和来源保护。

## C 一键 NPU 验收

两机使用同一提交、模型、依赖及配置。
31/32 可复用 `/home/cryang/mempool-min-ticket01-3132.json`。
若已有同名日志目录，换一个全新 run-id。
先启动 P，再启动 D，后续自动协调。

P 机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode reuse-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id ticket03-001
```

D 机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode reuse-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id ticket03-001
```

添加 `--describe` 可检查有效配置、内存预算和阶段，不启动硬件。
入口强制 `prefix_pages=40`、`verify_prefix=true`、`match_prefix=true`。
参考组独立初始化，关闭 mempool Router/命中，不污染目标组 prefix。
P eager，D Graph，默认页大小仅支持 128；输入为 token IDs，greedy。

| 用例 | 输入 | hit_len | 实际后缀 token | 新保存页 | 累计 prefix 页 |
|---|---|---|---|---|---|
| A | 4096 | 0 | 4096 | 32 | 32 |
| B | A + 1024 | 4096 | 1024 | 8 | 40 |
| C | A | 3968 | 128 | 0 | 40 |

验收模式主动留出一个 allocator 页，确保测试真实非连续恢复映射。
它还把 fill 提交延迟到全 TP 的 native_freed 发布之后。
日志必须证明此时 suffix 已完成、ready_tokens 仍为 0。
这些是明确的验收控制；普通 `serve` 且 `verify_prefix=false` 不注入。
报告保留这些控制，不声称已验证无注入服务的重叠性能。

每个 rank 核对 78 层主 KV 的两部分，以及 21 层 Index K。
核对基准来自 A 原始计算值及 B/C 后缀原始计算值。
恢复按真实 token 索引逐内容比较，报告附逐页 digest 便于关联。
B/C 的完整输出必须各自等于冷算参考；任何差异都失败，不放宽容限。
同时检查 D 实际 P/D 来源、Graph、全额度归还、显式 free 和超时 fail-stop。

日志位于两机 `/home/cryang/log/mempool-min/ticket03-001/`。
请回传两端 `summary.json`、P 的 `target/evidence.json`。
若失败，一并保留 `reference/`、`target/` 日志、`responses.json`、`snapshots.json`、
`identity.json`、`peer-identity.json`、`identity-diff.json` 和 timeout 目录。
入口失败返回非零，清理它自己启动的进程。

## D 本地验证与剩余验收

Mac 最终版本完整 CPU 套件 453/453 通过，38.513 秒。
12 个功能/验收模块 strict mypy 通过。
23 个 Python 文件通过 lint、format、isort、Python 3.9 AST。
入口 CPU 测试保留真实子进程的启动/回收，仅替换外部 NPU 程序。
原生接口测试执行实际 Req、extend 分配与 ChunkCache 清理方法。

Standards / Spec 最终均为 0 项未解决发现。
Standards 的 1 项进度摘要问题已修复，并同步 Markdown/HTML。

[检查记录](verification/ticket03-cpu-20261026/checks.json)保留源码摘要和实际测试结果，
[review 记录](verification/ticket03-cpu-20261026/review.md)分别报告两轴结果。
本地 CPU 使用 Python 3.12；mypy 按 3.10 检查，另做 3.9 语法兼容检查。

本机无 NPU；用户首轮硬件验收在 C 输出比较处失败，尚待诊断和重测。
物理 NUMA 落点沿用既有未验证标记，不改变 ticket02 已记录的限制。
本轮不是完整阶段 0 验收；E、完整拒绝矩阵与组合演示留给 04。
