---
title: Ticket02 重写：prompt prefix 保存
subtitle: 功能代码阅读顺序、所有权和双机自运行验收
template: doc
theme: blueprint
lang: zh
date: 2026-10-26
status: 实现完成；CPU 437 项通过；两轴 review 通过；NPU 待用户验收
---

基于用户已验收的 ticket01 `7275f29a05` 重写。
本票保存冷请求的 prompt 主 KV 和 Index K，所有请求仍为 `hit_len=0`。
代码中没有恢复、fill、evict 或 prefix 命中行为；它们仍属后续票。
ticket02 保持 open，ticket03 等待本票 NPU 验收。

## A 功能代码 review 顺序

**8 个已有功能文件修改，新增 1 个文件，全部位于 Ascend/NPU。**
未修改公共模型、scheduler、native allocator、HTTP 或 kernel 代码。
以下列表只含功能源码，测试与说明文件不计入。

| 顺序 | 功能文件 | 阅读目的 |
|---|---|---|
| 1 | [ascend/mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | `prefix_pages=0/40`、`verify_prefix` 和四类副本注册合同 |
| 2 | [npu/mempool/layout.py](../../python/sglang/srt/hardware_backend/npu/mempool/layout.py) | 新增三块 P 区域的几何、偏移和 probe 边界；D 区域不增加 |
| 3 | [npu/mempool/config.py](../../python/sglang/srt/hardware_backend/npu/mempool/config.py) | 由模型几何构造 alive Index K 和两块 prefix 区域 |
| 4 | [npu/mempool/manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 带区域偏移的 typed view；新增区域每层映射首尾校验 |
| 5 | [npu/mempool/prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | 本地页预留、异步复制、完成收据和源释放门控；显式验收模式的内容参考 |
| 6 | [npu/mempool/runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 实际 native Index K 索引写回、完成计数和事件；模型启动接线 |
| 7 | [ascend/mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 统一目标页号、TP16 完成汇总、pending 命令队列与实际 free |
| 8 | [ascend/mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 持久目录、容量选择、源与父路径保护、publish 和账本归还 |
| 9 | [ascend/mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 将真实 store 接到 SR；核对布局、容量和验收配置再注册 |

现有 Ascend attention hook 位于 Index K producer 写入之后。
runtime 在该 hook 中按 `out_cache_loc` 读取实际非连续 HBM 行并写到 P alive Index K。
既有 service 使用 runtime 的完成收据决定 native release，所以无需新增公共生命周期 hook。
功能阅读顺序补充 Standards / Spec review，不代替它们。
该范围约束及交付规范已记录在根目录 [AGENTS.md](../../AGENTS.md)。

## B 正常路径、所有权与完成条件

```sequence num
P_runtime -> P_alive: 主 KV 与 21 层 Index K 写回
P_runtime -> P_service: 设备事件完成且全部写入计数正确
P_service -> Main: native_freed，HBM/请求行已释放
D_service -> Main: finished，最后读写与 native 清理完成
Main -> Main: 保护待复制源及父路径，再撤 owner hold
Main -> SR_P: admit，实际源槽/页与 op_id
SR_P -> P_workers: 同一组 prefix 页号，逐 rank 异步复制
P_workers --> SR_P: 设备完成后目标可读
SR_P --> Main: TP16 admit_ack
Main -> Main: 先父后子 publish，解除操作锁
Main -> SR_P: free
Main -> SR_D: free
SR_P --> Main: 实际 alive 归还后的 free_ack
SR_D --> Main: 实际 alive 归还后的 free_ack
```

P native 清理和 D finished 的两种到达顺序都可推进。
Main 持有逻辑目录和额度；SR/control 持有真实 alive 槽；PrefixStore 持有本 rank 的 prefix 页。
native 请求行释放后，操作通过 rid/attempt/op_id 及实际源槽继续关联。

源引用按 owner hold `1→2→1→0` 变化；最后一个 rank 完成之前保留源与 writing 目标。
copy 使用 UniDexCopy 双设备指针，不在 CPU 解引用 BM 地址。

已有 prefix 在重复冷算时保留；新页只能接在可发布父路径后。
容量不足时缩短待保存尾部，满容量时跳过 admit。
错误或超时 fail-stop，保留 writing 页及受保护源；不制造 free_ack。
warmup 保持原生本机路径，不建立 alive 绑定，也不触发 prefix 写回或占用。

## C 自运行 NPU 验收

在两机已有 Ascend 容器中使用相同提交、模型、依赖和配置。
复制 [JSON 示例](../../ascend-mempool-test/configs/mempool-min.example.json) 为 `mempool-min.json`，
填写 P/D 地址、模型路径、网卡和 NUMA 节点。两侧内容必须一致。
使用同一个全新 run-id；P 先启动，随后启动 D，后续步骤自动协调。

P 机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode prefix-acceptance \
  --config mempool-min.json --run-id ticket02-001
```

D 机：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode prefix-acceptance \
  --config mempool-min.json --run-id ticket02-001
```

入口生成并保存有效 JSON：`prefix_pages=40`、`verify_prefix=true`。
保留原生 warmup，P eager、D Graph BS16、B128、P8192/D128、单真实请求。
`--describe` 只展示配置与容量，不启动 NPU。
`--mode acceptance` 仍用于 ticket01，要求 prefix 关闭。
常驻 `--mode serve` 读取 JSON；日常保存设置 `prefix_pages=40, verify_prefix=false`。

按实际 PoolLayout 计算 BM：P 为 13 GiB/rank、TP16 共 208 GiB；D 共 16 GiB。
内容验收额外为 P CPU 参考缓冲预留 32 GiB，两者合计检查至少 240 GiB 可用 host 内存。
这不包含其他进程后续用量；物理 NUMA 页落点仍标为未验证。

runner 自动执行复制适配器、独立冷算参考、目标服务、故障注入和自身进程清理。
参考组不加载 Main/SR/prefix 配置，四个用例都比较完整输出 token IDs。

| 用例 | prompt | 新保存页数 | 最终 prefix 页数/份 |
|---|---|---|---|
| A | 4096 token | 32 | 32 |
| B | A 加 1024 token | 8 | 40 |
| C | 与 A 相同 | 0 | 40 |
| E | 不同的 4096 token | 0 | 40 |

内容参考来自实际计算值，按 logical token 比较主 KV 和 Index K。
比较时点包括 alive 写回完成、native 释放后复制完成、alive 实际 free 后。
CPU 参考只保留当前请求与已存 40 页；验收读回与同步不用于性能结论。
逐页摘要再关联实际 alloc 槽、admit 源页、目标页和 publish 目录。
缺 rank、坏页号、内容差异、过早 ack/free、缺少完成记录或输出差异均失败退出。

主报告：`/home/cryang/log/mempool-min/ticket02-001/summary.json`。
回传两端整个同名运行目录：有效配置、identity、memory-budget、cases、responses、
snapshots、prefill/decode-normal.log、evidence.json、timeout 报告和最终清理日志。
正常用例后会暂停自身 P workers，额外等待一次 operation_timeout（默认 600 秒）。
两端只清理本次启动的子进程，最终清理检查通过后才成功退出。
报告保留 `user_accepted=false`，用户确认后才关闭 ticket02。

## D CPU 与静态检查

完整 CPU 套件 437/437 通过，35.966 秒。
首跑因本机测试路径缺 requests 失败，补回已有缓存依赖后完整重跑通过。

覆盖内容、TP 延迟、实际 Index K 索引、部分容量、重复 ack、错误留存、warmup 与旧冷请求回归。
验收检查器拆分后定向负向测试通过。
review 整改后相关 42 项定向回归及 5 文件 strict mypy 通过。

最终 13 个源/脚本 strict mypy，21 个 Python 文件 lint/format/isort/Python3.9 AST，
六组入口描述与两个 shell 语法检查通过。
完整记录见 [checks.json](verification/ticket02-rewrite-20261026/checks.json)。
NPU 尚未由本机执行。

## E Standards

Standards 复核通过，剩余发现 0 项。

- admit 已提取为 58 行的 `_admit`；`execute` 回到约 100 行，编排与复制校验职责清晰。
- 新增布局、操作状态构造及 runtime/SR 接口调用已补齐关键字参数。

本轮整改没有扩大功能文件范围，也未改变硬件验收门控。
先前 2 项非阻断建议均已处理；未发现额外 smell baseline 问题。

## F Spec

未发现剩余规格偏差，0 项阻断问题。

已核对实际 Index K 池布局、producer→hook 顺序、完成门控、独立区域、TP16 收据、
父页发布和显式 free，以及 A/B/C/E、部分容量、重复回执、warmup 和失败留存。
`prefix-acceptance` 的独立参考、内容读回、实际槽页关联、完整输出和进程清理均有检查。
短 receipt pages 被 zip 截断的问题已增加长度检查，负向用例先失败再通过。
未引入 ticket03 的命中恢复、fill 或 evict。

Standards 剩余 0 项；Spec 剩余 0 项。
NPU 内容、Graph 与实际设备可见性仍待用户验收。
