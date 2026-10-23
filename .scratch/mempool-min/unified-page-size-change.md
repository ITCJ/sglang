---
title: 统一 token 页大小：改动与验证清单
subtitle: 2026-10-24 · M54 / C100 · ticket01 保持 open
template: doc
theme: blueprint
lang: zh
date: 2026-10-24
status: 代码、设计源与CPU检查完成；用户NPU验收待执行
---

## A 统一约束与交付状态

DRAM 逻辑页与原生 HBM 页严格共用可配置的 `page_size=B`。
sparse、dense、P/D、alive/prefix、Index K 相关池都遵守此约束。
整数倍关系不能代替相等。本次决定取代旧 sparse 64/128 约定。

当前 GLM-5.2 mempool-min 集成默认并仅允许 `B=128`。
`SUPPORTED_PAGE_SIZES=(128,)` 是此集成的开放范围，不是全部 Ascend 硬件的能力表。
后端默认值见 `hardware_backend/npu/utils.py`；实际池、allocator 和传输布局也须相等。
其他值须完成对应后端、池与 kernel 的能力验证后再开放。

dense 的设计、检查点及跟踪间隔已统一；dense 服务集成仍属于后续工作。

开始时核对分支 `cryang/dev/mempool-min`，HEAD 为 `af517b1a23a115d19fddd639e249c73e44a7614f`。
原有未提交工作保留。本次以会话开始时的文件快照统计，共修改或新增仓库内 37 个文件、设计源 17 个文件。
仓库内包含 5 个功能文件、3 个启动/验收脚本、6 个测试文件、21 个说明及图解文件和 2 个验证证据文件。

本次没有新增公共主线功能改动。原 ticket01 已有的公共接缝继续使用实际 allocator 页大小。
两次 UniDexCopy 的 sparse fetch 按 token 索引搬运，调用链未发现页粒度冲突。
OS 内存页、BM 字节对齐、64 字节探针、512/64 特征维、kernel chunk 和 Graph padding 均另有含义。

## B 旧公式与新公式

以下数值例统一取 `B=128`。`L` 是 prompt token 数，`G` 是生成预留上限。
最小 demo 的输入及整槽容量必须按 B 对齐；完整设计另保留尾页场景。

| 项目 | 旧口径 | 新公式与本例 |
|---|---|---|
| DRAM / HBM token 页 | 64 / 128 | 同一个 B / B |
| hash、目录页与输入边界 | 按 64 分块 | 按 B 分块；完整页数 L/B |
| 4096-token prompt | 64 个逻辑页 | 4096/B = 32 页 |
| 5120-token prompt | 80 个逻辑页 | 5120/B = 40 页 |
| 相同完整 prompt 的命中上限 | 4032，最后 64 token | B×floor((L−1)/B)；4096 时为 3968，重算最后 128 |
| prefix 容量 | 80×64 = 5120 token | (5120/B)×B；本例 40×128，容量不变 |
| A/B/C/E 后 prefix 占用 | 64→80→80→80 页 | 32→40→40→40 页；E 仍遇到 prefix 已满 |
| P alive 整槽 | 8192/64 = 128 页 | S_P/B = 64 页；有效 prompt 仅前 32 页 |
| D alive 整槽 | 128/64 = 2 页 | S_D/B = 1 页 |
| 16 槽 P/D 账本 | 2048 / 32 页 | 16×S_P/B = 1024；16×S_D/B = 16 |
| slot s 的逻辑页 | s×(capacity/64)+i | s×(capacity/B)+i |
| native 预留 | HBM 独立按 128 算 | ceil((L+G)/B)；4096+128 仍为 33 页，5120+128 为 41 页 |
| byte 预算 | 单页 token 数单独硬编码 | 页数×B×每 token 字节×层数×物理副本数 |
| Index K 单页 | 单独的 HBM 边界 | B×256 字节/producer 层；主 KV 的特征维不随 B 改 |
| dense 状态 | 128-token 检查点 | B-token 检查点与跟踪间隔；kernel 内部 chunk 独立 |

小例仍用 256/384-token prompt、512-token P 槽、128-token D 槽。
prefix 保持 384 token，改为 3 页；A/B 有效页为 2/3，C 命中 128 后重算 128。
完整设计保留 8200-token 尾页例：65 个 prompt 页，尾页已有 8 token。
生成预留 512 时，DRAM 生成区需 5 页，native 总计 69 页，其中新增 4 页。

旧“4032 命中、HBM 半页接续”已撤出当前验收。
提前 EOS、实际尾页、未用整页与未用预留仍须正确清理。
native 释放保存的完整索引集合，发生在 drain 之后，并且只释放一次。

## C 功能代码与启动脚本逐文件说明

以下路径均相对实现仓库根目录。
前四个功能文件在 `python/sglang/srt/disaggregation/ascend/`。

| 文件 | 本次具体修改 |
|---|---|
| `mempool_router_protocol.py` | 配置开放范围、整槽对齐检查；page_hashes 显式接收 B 并传给生产 storage hash |
| `mempool_router.py` | 注册严格检查 DRAM/HBM B；输入、hash 数、目录、整槽账本和 native 预留均由 B 推导；检查 alloc_ack 页大小及实际预留 token 数 |
| `mempool_router_transport.py` | 启动参数由 RouterConfig 派生；注册前核对真实 worker、DRAM config、allocator、KV 池和 Index K 传输布局；注册实际 B |
| `mempool_subrouter.py` | SR 显式接收 B；分配前拒绝请求不一致；页表数量、逻辑页号和描述符从 B 推导；核对 native allocator |
| `python/sglang/srt/hardware_backend/npu/mempool/config.py` | 保存并传递 server_args.page_size；建池前检查正整数和容量对齐 |
| `ascend-mempool-test/scripts/run_mempool_min.py` | 统一 PAGE_SIZE 环境变量；B 对齐 fixture/负向请求；把 B 与 P/D 容量交给报告检查 |
| `ascend-mempool-test/scripts/run_service.sh` | PAGE_SIZE 为规范入口；兼容旧 HBM_PAGE_SIZE 名称，但同时出现且不相等时退出 2 |
| `ascend-mempool-test/scripts/verify_mempool_min.py` | 检查两侧 32 rank 实际注册、整槽账本、逻辑页表、native 页/token 数；提前 EOS 输入按 B 对齐；报告记录 B |

`mempool_native_reservation.py` 已使用真实 `allocator.page_size`，本轮无需修改。
预留保存非连续索引，Req 消费该预留，最终释放完整集合。
Index K 现有原生传输通过实际 transfer_layout 和 native 索引绑定，新增一致性检查保护该路径。
prefix Index K 保存/恢复尚未实现，其 B 公式和验收要求已更新在 tickets 02–03。

## D CPU 测试逐文件说明

以下六个文件均在 `ascend-mempool-test/tests/unit/`。

| 文件 | 本次覆盖 |
|---|---|
| `test_config.py` | NPU mempool 的实际 B 传递和容量对齐 |
| `test_mempool_router.py` | 支持范围、32/40 页、旧 4032 输入拒绝、hash 的 B 与 seed、P/D rank 不一致、64/1 整槽额度、native 预留与归还 |
| `test_mempool_subrouter.py` | B 页描述和逻辑页号、不同 slot、不一致请求在分配前拒绝、allocator 不一致 |
| `test_mempool_native_reservation.py` | B=128 的 native 页预留；提前结束、尾页及未用预留释放 |
| `test_mempool_native_hooks.py` | 实际 scheduler、DRAM、allocator、KV 池、Index K 传输布局一致性；更新合法配置 fixture |
| `test_mempool_min_entry.py` | PAGE_SIZE 传播、验收页证据、拒绝旧注册/页表/账本/native 报告口径 |

完整套件覆盖其他既有控制、进程回收和传输行为，结果为 416/416 通过。
Mac CPU 环境未执行 Linux 原生 storage hash 扩展。
CPU 已验证生产 hash 调用的 B 参数、命名空间 seed 和页数合同；真实扩展仍由 NPU 服务验收覆盖。
本轮不把尚未实现的 prefix 命中功能列为 CPU 已通过。

## E 仓库说明、票与图解逐文件说明

下表括号中的 md/html 各算一个文件。
原有历史 Comments 保留，当前正文与新增决定记录分别处理。

| 文件 | 本次具体修改 |
|---|---|
| `CONTEXT.md` | 追加 token page、统一 B 与其他页/对齐概念的领域定义 |
| `.scratch/mempool-min/discussion.md` | 追加统一页决定、支持范围、公式与旧决定的替代关系 |
| `.scratch/mempool-min/SESSION-START.md` | 新 session 的统一约束、实际 HEAD、CPU 结果和验收门控 |
| `.scratch/mempool-min/spec.md` | 统一合同、预算、hash/目录、注册、prefix 与 A/B/C/E、V1–V10 |
| `.scratch/mempool-min/ticket-plan.md` | 四票职责与新命中边界、40 页容量及依赖关系 |
| `.scratch/mempool-min/ticket-01-implementation-plan.md` / `.html` | native 与 DRAM 同 B；64/1 整槽预算、32 个有效页、部署和审核公式 |
| `.scratch/mempool-min/sr-scheduler-explained.md` / `.html` | SR 页表、整槽分配、真实绑定与 B 的关系 |
| `.scratch/mempool-min/stage-0-ticket-review.md` / `.html` | 四票数据流与验收改为完整页边界，移除旧半页要求 |
| `.scratch/mempool-min/target-alignment.md` / `.html` | 逐步骤更新 D 部分，区分实现、CPU 和用户 NPU 验收 |
| `.scratch/mempool-min/issues/01-cold-request-lifecycle.md` | B 全链路注册及页数检查；补充本轮验证记录，保持 open |
| `.scratch/mempool-min/issues/02-prompt-prefix-save.md` | 40 页 prefix、B 派生字节预算与 Index K 保存要求 |
| `.scratch/mempool-min/issues/03-prefix-restore-and-suffix.md` | C 命中 3968、31 个完整页、最后 128 重算；保留实际尾页清理 |
| `.scratch/mempool-min/issues/04-full-demo-acceptance.md` | A/B/C/E、V1–V10 统一，保持容量不足场景及下游门控 |
| `ascend-mempool-test/MEMPOOL_MIN.md` / `.html` | 当前修正与原 ticket01 的 34 文件交付分开说明；更新配置、审核与验收说明 |
| `.scratch/mempool-min/unified-page-size-change.md` / `.html` | 本清单：完整修改范围、公式映射、验证与 review 顺序 |
| `.scratch/mempool-min/verification/unified-page-size-20261024/cpu.log` | 本次完整 416 项 CPU 输出 |
| `.scratch/mempool-min/verification/unified-page-size-20261024/checks.json` | 静态、启动、图解检查和设计源写回状态；明确 NPU 未执行 |

## F syncfile 设计源逐文件说明

以下路径相对 `/Users/hibikid/Documents/syncfile/sglang-kv-explainer/`。
17个文件已写回；写回前核对原文件，写回后逐文件验证SHA-256一致。

| 文件 | 本次具体修改 |
|---|---|
| `README.md` | 当前 C100/M54 决定、现行图解和历史入口 |
| `handover/handover-report.md` | 追加统一约束与实现边界 |
| `mempool-design/requirement.md` | 保留原需求，追加新决定及替代关系 |
| `mempool-design/discussion.md` | 更新现行决策索引，追加 C100，历史各轮讨论保留 |
| `mempool-design/walkthrough.md` | sparse/dense、页号、哈希、容量、尾页、HBM/Index K 与检查点公式统一 |
| `mempool-design/copy-perf-tests.md` | 更新 B-token 页及其字节预算；保留独立的 sparse fetch chunk 与 batch 设置 |
| `mempool-min-demo/requirement.md` | 保留原需求，追加统一页约束和范围 |
| `mempool-min-demo/discussion.md` | 更新现行索引，追加 M54，保留历史 64/128 讨论 |
| `mempool-min-demo/walkthrough.md` | 小例与 4K/5K 的页数、命中、prefix 容量、注册、dense 检查点同步 |
| `mempool-html/plan-full-demo.md` | 当前图解计划与历史计划分开，指向新源 |
| `mempool-html/plan-min-demo.md` | 当前图解计划与历史计划分开，指向新源 |
| `mempool-html/unified-full-demo.md` | 新建完整设计图解源，含 B 计算器、尾页与 dense 流程 |
| `mempool-html/unified-min-demo.md` | 新建最小 demo 图解源，含小例、A/B/C/E、容量与 B 计算器 |
| `page/mempool-full-demo.html` | 从现行源重新生成图解；公式计算器只接受当前开放的 B |
| `page/mempool-min-demo.html` | 从现行源重新生成图解，保留请求规模及 prefix 满场景 |
| `page/history/mempool-full-demo-pre-unified-pages.html` | 原交互页历史快照，增加明确的失效口径提示 |
| `page/history/mempool-min-demo-pre-unified-pages.html` | 原交互页历史快照，增加明确的失效口径提示 |

原图解有大量写死页数的交互状态，本轮以新的图解和计算器作为现行页面。
旧交互过程可通过 history 快照追溯；当前详细设计以 walkthrough 为准。
历史快照中的 64/128、4032 仅描述旧方案，不能作为现行验收要求。

## G 实际检查与用户 NPU 验收

| 检查 | 结果与边界 |
|---|---|
| 完整 CPU unittest | 416 项通过，23.280 秒；详细输出见本地 cpu.log |
| strict mypy | 5 个功能文件与 2 个 Python 启动/验收脚本通过 |
| Ruff / Python AST | 13 个修改的 Python 文件通过；语法按 Python 3.9 检查 |
| Shell 语法 | run_service.sh 与 run_mempool_min.sh 通过 |
| P/D 启动 dry-run | 两侧均产生 --page-size 128；PAGE_SIZE 与旧别名冲突时退出 2 |
| 自运行入口 describe | 配置传播为 PAGE_SIZE=128；展示完整阶段与日志位置 |
| 图解 JS 与公式 | 两页各 6 组公式、7 个不支持 B、容量对齐、历史提示检查通过 |
| HTML 生成 | 目标页、方案、回顾、SR 讲解、审核指南及两套设计图解均生成；STE 无警告 |
| 页面查看 | 已在浏览器查看最小 demo 新图解和计算器；公式另由脚本核对 |
| git diff --check | 通过 |
| 设计源写回 | 17个文件写回成功，SHA-256核对一致；原讨论及旧交互图历史快照保留 |
| NPU 硬件 | 未执行；不能据 CPU 结果关闭 ticket01 |

NPU 使用两机相同代码和相同 `mempool-min.json`，设置实际模型、地址、NUMA 和网卡。
配置 `page_size` 保持 128，两侧同一个新的 run-id。
在 P 容器执行：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode acceptance --config mempool-min.json --run-id unified-page-001
```

在 D 容器执行：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode acceptance --config mempool-min.json --run-id unified-page-001
```

入口自动完成环境装配、就绪等待、请求、对比、报告、超时场景和自身进程清理。
失败返回非零；NPU 日志与报告保存在 `/home/cryang/log/` 下的独立运行目录。
这次 acceptance 是 ticket01 冷请求验收；smoke 或 serve 不构成完整验收。

后续 tickets 02–04 仍须分别验收 prefix 保存/恢复、C 命中边界和 E 容量不足。
ticket01 保持 open，未解锁下游。

## H 功能代码 review 顺序

1. `mempool_router_protocol.py`：唯一 B、支持集合、hash 参数和容量约束。
2. `hardware_backend/npu/mempool/config.py`：server_args 到真实 DRAM 配置的传递。
3. `mempool_router_transport.py`：启动参数、实际池/allocator/传输布局与逐 rank 注册。
4. `mempool_router.py`：入口、账本、目录、请求、预留和 alloc_ack 验证。
5. `mempool_subrouter.py`：分配前检查、整槽页表、逻辑页号和 native 绑定。
6. 对照未改的 `mempool_native_reservation.py`：尾页、预留消费、drain 后完整释放。
7. `run_service.sh` → `run_mempool_min.py` → `verify_mempool_min.py`，随后阅读六个相关测试。

详细阶段状态见 [target-alignment](target-alignment.html) 的 D 部分。
硬件通过后仍需用户确认对应 ticket 的验收，才可关闭并推进依赖票。
