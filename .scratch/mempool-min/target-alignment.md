---
title: mempool-min：实现目标对齐核对
subtitle: 以 syncfile 现行文字为依据，区分阶段 0 与后续目标
template: doc
theme: blueprint
lang: zh
date: 2026-10-26
status: ticket01 已关闭；02 待关闭确认；03 两轮 C 输出失败，正在定位首层计算
---

**当前四票与 sparse 阶段 0 一致，但没有覆盖后续全部目标。**
alive 整槽分配和私有副本都有阶段 0 的明确依据。
K1 共享、真正的 alive 页分配及 dense 集成仍须承接。
此前说明没有充分区分阶段终点与整体目标。

## A 核对依据与结论

用户本轮指定 syncfile 内的 mempool-min-demo 为最终实现目标。
现行依据是 [walkthrough.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/walkthrough.md)。
第 0–10 节定义 sparse 流程及后续阶段，第 11 节定义 dense 轨道。
[discussion.md](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/discussion.md) 用于追溯 M1–M54。
现行正文与图解按2026-10-24的M54/C100统一页决定同步。
历史候选及历史图解快照不覆盖现行文字。

| 核对项 | 现行设计 | 本地规格与方案 | 判断 |
|---|---|---|---|
| Main Router / SR | 独立 Main；rank 0 协调；每 worker 一个分片 | 相同；网络线程只入队，tick 内通信 | 阶段 0 一致 |
| 统一 token 页 | sparse/dense、DRAM/HBM、P/D与相关池严格共用B | 当前GLM-5.2集成仅放行128；注册核对实际各组件 | ticket01 已验收；新增区域随 02／03 验证 |
| page 与 slot | alive 用临时整槽后端；prefix 按页分配 | 相同；整槽扣账、free_ack 后归还 | 阶段 0 一致 |
| 四类副本数 | 阶段 0 为 16 / 16 / 16 / 16 | 相同；ticket01 尚未启用的区域如实注册 | 阶段 0 一致 |
| 就绪与释放 | 三条件启动 D；finished 与 free_ack 分开 | 相同；接管旧分配、绑定与自动释放 | 阶段 0 一致 |
| prefix 与验收 | 保存、恢复、后缀；A/B/C/E、V1–V10 | 分布在 02、03、04 | 阶段 0 一致 |
| K1 与后续阶段 | 阶段 0 / L0 → K1 → 阶段 1 | 四票之后缺完整承接清单 | 任务覆盖缺口 |
| dense 轨道 | 单独的共享 prefix 与状态交接流程 | 保留目标，尚未拆本仓库集成票 | 后续覆盖缺口 |

这是文档范围核对，不代表这些设计已经实现。
2026-10-24统一页决定取代此前的64/128分离；本轮已修正现行正文和ticket01代码。
HTTP/ZMQ、原生预留和冷请求已通过 ticket01 用户 NPU 验收。
prefix 保存已由 ticket02 实现并通过 CPU 与自动化 NPU 验收。

ticket03 已实现命中恢复与后缀计算，CPU 回归通过。
NPU 两轮在 C 的完整输出比较失败，首差已限制到第 0 层至第 1 层的计算。

## B 最小空闲 slot 0 与 page 的关系

依据：walkthrough 第 0、3、5 节，M17、M31、M40。
阶段 0 的 DRAM 逻辑页与原生 HBM 页统一为可配置的 B token，当前部署 B=128。
alive 暂时按整槽分配，页表描述其中的有效数据。

以 S_P = 8192、prompt = 4096 为例：

| 项 | 数值与含义 |
|---|---|
| 一个 P 存储槽 | 8192 / B = 64 个逻辑页额度 |
| slot 0 | 对应池内逻辑页 0–63 |
| 本次有效数据 | 使用 slot 0 的前 32 页，即页 0–31 |
| 剩余尾部 | 页 32–63 仍被本请求占住，不能再次分配 |
| 释放 | 最后一项依赖完成后归还整槽，收到 free_ack 才恢复额度 |

“最小空闲槽”表示从空闲槽中选编号最小者。
全空时选 0；若 0 被占用且 1 空闲，则选 1。
这个规则用于明确验证归还后的复用。
它既不是“最小空闲页”，也不是原生 req_pool_idx。

```text
阶段 0 alive：逻辑页接口 → 连续存储槽临时后端
阶段 0 prefix：逻辑页接口 → 真正的页分配器
阶段 1：调整为 page-first 布局
阶段 3：alive 替换成真正的页分配器，读路径支持散页
```

page-first 改变数据排列；页分配器改变分配和回收单位。
两项工作分别安排在阶段 1 和阶段 3。
原生 HBM 与 DRAM 注册同一个 B；只满足整数倍关系仍须拒绝。

## C 两个 rank 共用 NUMA 与共用一份 KV

依据：walkthrough 第 0、3、8.3 节，M12、M20、M37。
NUMA 分配决定请求在哪个节点分配内存。
共享决定两个 rank 是否映射同一份物理数据。
只把 rank 0、rank 1 放到 NUMA 0，不会自动合并副本。

| 示例 | 阶段 0 私有副本 | K1 共享目标 |
|---|---|---|
| rank 0 和 rank 1 | 各持有一份完整主 KV | 同属共享组，持有同一份主 KV |
| 逻辑 page 7 | 页号相同，背后是两份物理内容 | 组内同一逻辑页对应同一份物理内容 |
| 写入 | 两个 rank 各完整写自己那份 | 按配置分工写入，汇总后供所有 reader 读取 |
| 地址 | 各进程解析自己的映射 | 共享物理内容，进程虚拟地址仍可不同 |
| TP16、每 NUMA 两个 rank | 每侧 16 份 alive 主 KV | 每侧 8 份 alive 主 KV |

K1 的四类副本数必须分别处理：

| 数据类型 | 阶段 0 | K1 |
|---|---|---|
| alive 主 KV，R_am | 16 | 8；默认每组 2 个 rank |
| P alive Index K，R_ai | 16 | 1 |
| prefix 主 KV，R_pm | 16 | 1 |
| prefix Index K，R_pi | 16 | 1 |

D 生成的 Index K 在这两个阶段仍只留在 HBM。
K1 还包括单份 prefix 的 load_back 读取与广播。
SR 分片数量由 worker 数量决定，不等于物理副本数。
共享后分片解析组映射；Router 仍只处理逻辑位置和不透明描述符。

## D 名称、验收与实施票怎样对应

**2026-10-26 ticket03：第 25 步，首层诊断交付完成 CPU 验证。**

完整 CPU 回归 464/464 通过（38.537 秒），4 模块 strict mypy、7 文件静态检查通过。
Standards / Spec 均为 0 项未解决；真实 Ascend prepare/core/attention 接线与原输出保留已复核。
用户下一轮运行 ticket03-003、--diagnose-reuse，回传 REUSE_OPERATORS 与错误。

[检查记录](verification/ticket03-002-20261026/checks.json)区分 CPU 诊断验证和待运行 NPU 对照。
模型根因尚未确定，严格完整输出验收继续失败；03 保持 open，04 不解锁。

**2026-10-26 ticket03：第 24 步，首层诊断摘要与内存预检已接入。**

摘要区分 prefix 内容证据与首层算子证据，缺检查点、重复或串错请求时不标记完整。
终端新增 REUSE_OPERATORS；保留每个 rank 的检查点与三个算子对照结果。
额外 CPU 检查点限制为每 rank 64 MiB，内存预检计入该上限。新摘要回归与完整验证待完成。

**2026-10-26 ticket03：第 23 步，首层探针已接入真实 Ascend 计算路径。**

检查点覆盖第 0 层输入、query/RoPE、top-k 顺序/集合、attention、两段输出投影和第 1 层输入。
原始 attention 返回值保留；仅冷算 A 的第 0 层运行两次额外算子对照，C 与该尾页对照比较。
全部接线位于 Ascend/NPU，默认关闭。实际调用链 CPU 验证与摘要接线待完成，NPU 尚未重测。

**2026-10-26 ticket03：第 22 步，首层计算诊断器已实现，接线待完成。**

ticket03-002 用户回传完整诊断：16 rank 身份/位置/长度/页表均通过，第 0 层 KV 一致。
主层 1 起 77 层、Index 层 1 起 20 层不同，无非有限值；各 rank 首差统计相同。
[回传记录](verification/ticket03-002-20261026/record.json)保留原值与推断范围。

新增首层 query/top-k/输出检查点，以及同形状重复和最后一页 query 的算子对照。
只保存 CPU 尾页，额外算子结果不送入模型。7 项诊断 CPU 测试通过，服务接线仍待完成。
该测试验证诊断器，未在 Mac 复现 NPU 模型根因；完整输出失败与 open 状态保留。

**2026-10-26 ticket03：第 21 步，临时诊断交付完成 CPU 验证。**

review 修正后完整 CPU 回归 459/459 通过（38.333 秒）；5 模块 strict mypy、8 文件静态检查通过。
Standards 最终 0 项，Spec 的 1 项摘要关联问题已修复，最终 0 项未解决。
双机入口可用 --diagnose-reuse 自动打印摘要；下一轮由用户运行 ticket03-002。

这是诊断探针交付，未修复或复现 NPU 模型计算问题。03 保持 open，04 不解锁。
[本轮检查](verification/ticket03-001-20261026/probe-checks.json)记录实际执行范围。

**2026-10-26 ticket03：第 20 步，review 修正诊断证据关联。**

摘要现在核对 A 的 baseline、A/C 完整输入摘要、C 的 rid/reference_rid 和全部元数据检查项。
缺 baseline、缺/错输入摘要、错来源、错请求、缺检查项的六个反例先失败后通过。
首次完整 CPU 回归 458/458 通过；此项 review 修正后还需回归，NPU 根因仍待诊断。

**2026-10-26 ticket03：第 19 步，双机入口增加临时诊断开关。**

reuse-acceptance 增加 --diagnose-reuse，只在 target 的 P 服务启用探针。
两机身份校验包含开关；内存预检计入一页副本，失败清理后也生成 JSON 与终端摘要。
严格输出比较和非零失败退出保留。入口与汇总器的 CPU 验证待完成，NPU 仍待用户重测。

**2026-10-26 ticket03：第 18 步，临时诊断已接入 P 的实际计算路径。**

显式诊断开关与 verify_prefix 同时开启时，记录实际 sparse attention 的 query/KV 长度、位置和完整页映射。
从已验证的 computed-source CPU 数据取尾页，按相同完整输入逐层比较；普通路径默认关闭。

16 项相关 CPU 检查通过；尚待自运行入口、汇总器与 NPU 实测，未认定模型根因。

**2026-10-26 ticket03：第 17 步，临时诊断比较器已实现。**

新增默认关闭的尾页比较器，保留首个冷算 prompt 的一页 CPU 副本。
只比较相同完整输入；记录每层差异数量、最大绝对差和 RMS，并核对位置与页映射。
两项 CPU 诊断测试先失败后通过；它们验证诊断器，不代表 NPU 模型问题已复现或修复。

该步仅覆盖诊断比较器；完整输出相等的标准保持不变。

**2026-10-26 ticket03：首轮 NPU 失败，差异已定位到 C 重算尾页。**

用户回传 31/32 截图，commit=0308cd0814、run-id=ticket03-001。
入口进入 target 后，在 reuse-prefix-c 的严格输出比较处退出；D 收到 peer failed。
用户回传 A/B 完整输出相等，C 从 output[15] 开始不同；冷算 A/C 完全一致。

离线 reuse 内容检查通过：16 rank、78 主层、21 Index 层、真实非连续恢复与提前释放。
各 rank 的 A/C alive 都有 32 页，仅第 31 页不同；该页是 C 重算的最后 128 token。
suffix 检查证明写回等于本次计算源，不能证明与冷算值相等。
最终综合 Graph/生命周期汇总及 timeout 验收未完成。

根因仍未确定，需补充算子元数据与逐层差异幅度。未放宽完整输出比较。
[失败记录](verification/ticket03-001-20261026/failure-record.json)保留用户反馈与未知项。
03 继续 needs-info/open，04 不解锁。

**2026-10-26 ticket03：第 16 步：实现交付就绪。**

最终完整 CPU 回归 453/453 通过（38.513 秒）。Standards / Spec 两轴最终均为 0 项未解决发现；进度摘要问题和两处完成边界修正已验证。

源码阅读顺序、所有权、双机 reuse-acceptance 命令及回传材料已整理到 ticket03-delivery。

03 保持 needs-info/open，等待用户执行本票 NPU 内容、完整输出、D Graph 和释放验收。
02 关闭确认独立保留，04 未解锁；后续硬件修正另行提交。

**2026-10-26 ticket03：第 15 步：review 中修正完成边界。**

范围错误快照已移到设备完成事件之前，保证收据覆盖全部验证数据。命中证据检查允许快 rank 在等待慢 rank 时重复返回同一操作的本地完成记录；Main 的 ack 与解锁仍必须各一次。新增回归先失败后通过。
继续两轴 review；NPU 仍待用户执行。

**2026-10-26 ticket03：第 14 步：完整 CPU 回归通过。**

完整 CPU 套件 453/453 通过（38.884 秒）。12 个功能/验收模块 strict mypy 通过，23 个修改或新增 Python 文件 lint、format、isort 与 Python 3.9 AST 通过。恢复失败和提前 ready 的负向测试确认保留账本与来源锁。
实现与自运行入口已齐备；正在进行 Standards / Spec 两轴 review。NPU 尚未执行，03 保持 open，04 未解锁。

**2026-10-26 ticket03：第 13 步：原生接口集成检查。**

CPU 使用真实 Req 匹配、alloc_for_extend、行映射写入和 ChunkCache 清理方法，验证 C 的 3968 个恢复索引接续 128 个新算位置。31 个恢复页与最后新页均由原生清理归还，无重复释放。
已完成此集成检查；完整 CPU 回归、两轴 review 和用户 NPU 验收仍待完成。

**2026-10-26 ticket03：第 12 步：双机自运行命中入口。**

run_mempool_min.py 新增 reuse-acceptance，自动启动独立 A/B/C 冷算参考和命中目标组。两组完整 output IDs 必须相等。
入口检查 16 ranks 内容、真实后缀计数、D Graph、账本和释放，保留超时 fail-stop 与自身进程清理。
入口本地 CPU 测试已验证三条真实 decode 参考及模式配置；NPU 尚未执行，仍需用户每机一条命令运行。实现待完整回归与两轴 review。

**2026-10-26 ticket03：第 11 步：命中证据核验。**

新增 A/B/C 命中证据检查，逐 rank 关联原始 prefix 内容、真实恢复索引、fill、后缀和 alive；要求保护/解锁 op_id 独立且完成顺序正确。CPU 证据测试通过，并拒绝缺 rank、连续地址伪造、错误内容和提前 ready 等损坏记录。
硬件证据尚未生成；下一步接入自运行入口。

**2026-10-26 ticket03：第 10 步：验收边界控制。**

verify_prefix 且 match_prefix 的验收模式通过原生 allocator 分配真实索引，主动留出一页。全 TP 发布 P native_freed 后才提交 fill。正常服务不注入延迟。CPU 分配、service 与 TP tick 的 9 项测试通过，验证后缀先完成仍不能提前 ready。
这些控制用于 NPU 验收可重复地覆盖非连续映射和先释放后 fill；不是 NPU 已通过证据。

**2026-10-26 ticket03：第 9 步：原生 HBM 布局适配。**

load_back 已通过原生 MLA 池接口恢复 K/rope，支持普通布局与 FIA NZ 布局。CPU 测试用实际原生索引验证非连续页地址、512/64 拆分和 NZ 物理布局；相关 17 项测试通过。
NPU 验证尚未执行；后续补齐 ticket03 一键验收入口及源码审查。

**2026-10-26 ticket03：第 8 步，service 联合就绪与启动安装完成。**

service 已合并 fill 和后缀真实完成，原生 row detach 后仍保留后缀范围收据。CPU 测试实际清空 native 缓冲后继续完成 fill，确认此前不发布 ready；相关 service 14项通过。生产启动按 match_prefix 安装 Ascend ChunkCache 适配器。尚需原生接口集成负向检查、命中自运行入口及完整 review。NPU 未执行。

**2026-10-26 ticket03：第 7 步，两段内容证据通过 CPU 验证。**

验收模式分别核对 fill 和 suffix，最终用 A 的原始保存值与 B 的新算值核对完整 alive。load_back 内容检查按原生索引比较主 KV 两部分及 Index K，并记录实际 prefix_indices。prefix 7项通过；生产 service 联合就绪和 NPU 入口尚待接线。NPU 未执行。

**2026-10-26 ticket03：第 6 步，后缀独立范围和真实写回计数通过 CPU 验证。**

runtime 用 write_start 标识后缀起点，submitted／completed 仍从零累计真实写入。设备范围检查拒绝错位或额外行，detach 后保留范围与完成收据。runtime／prefix 共25项通过，含修复首轮额外行检查回归。service 两段就绪、生产接线和验收报告仍待完成。NPU 未执行。

**2026-10-26 ticket03：第 5 步，全 TP 恢复与 fill 协调通过 CPU 验证。**

SR 已接入两种复制操作，校验实际来源、请求绑定与统一页。TP16 行为测试覆盖最后 rank 延迟：完成前不发恢复／fill ack，也不发布 ready；native release 可以独立推进。SR 与原生适配器共8项通过。service 后缀进度、生产启动安装和 NPU 内容验收仍待完成。NPU 未执行。

**2026-10-26 ticket03：第 4 步，原生恢复所有权适配器通过 CPU 验证。**

新增 PrefillRestore，按实际 allocator 分配命中段，并通过 ChunkCache 接口交给原生 extend；恢复页归本请求所有。C 的31页非连续恢复与后续一页分配测试先失败后通过。SR／service 安装、实际原生释放集成与设备验收仍待完成。NPU 未执行。

**2026-10-26 ticket03：第 3 步，Main 命中与恢复计划通过 CPU 验证。**

Main 新增 match_prefix 开关，B／C 分别命中4096／3968，仍预留完整请求额度。load_back 与 fill 各自保护来源并按真实 ack 解锁；恢复 ack 后才发 fill 和启动模型请求。完整 ready 必须已有全 rank fill 回执；结束只增缺失页。

Router 16 项通过。SR 执行、原生索引接线、后缀计数和 NPU 入口仍待完成。NPU 未执行。

**2026-10-26 ticket03：第 2 步，load_back 复制适配器通过 CPU 验证。**

新增 prefix → 原生 HBM 主 KV 两部分与 Index K 恢复，使用逐 token 索引并检查完整页、非连续分配和目标边界。新增测试先失败后通过，prefix 6 项通过。复制回执等待设备完成；原生 allocator 接线、Main／SR、后缀就绪与 NPU 内容证据仍待完成。NPU 未执行。

**2026-10-26 ticket03：第 1 步，fill 复制适配器通过 CPU 验证。**

新增 prefix → 新请求 P alive 的主 KV／Index K 复制，使用独立 NPU stream 和真实事件回执；pending 时禁止归还请求来源。保存后再填入另一槽的行为测试先失败后通过，prefix 文件 5 项通过。load_back、命中协议、原生映射、后缀就绪和自运行验收仍待实现；NPU 未执行。用户已明确授权实施 03，02 仍待关闭确认，例外仅限 03。


**2026-10-26 ticket02 重写：reference 修复完成 CPU 全回归与两轴 review。**

本次完整 CPU 套件 440/440 通过，36.115 秒；2 个验收脚本 strict mypy、4 个修改 Python 文件 lint/format/isort/Python3.9 AST 通过。Standards / Spec 均为 0 项发现。

修复仅调整 reference 的请求完成计数合同，生产功能源码未改。已更新交付命令，31/32 复用既有配置并使用 ticket02-002 重跑；保留 ticket02-001 日志。本票 NPU 尚未验收，02 保持 open，03 继续等待。

**2026-10-26 ticket02 重写：reference 计数合同负向验证通过。**

prefix reference 正向回归通过，旧混合矩阵仍按原规则检查。新增负向用例确认意外 zero-decode、缺请求、多请求、缺 rank、Graph、写回、drain 与释放证据仍失败。11 项 service gate 检查及 2 文件 strict mypy 通过；完整 CPU 回归和两轴 review 进行中。

本修复仅涉及验收脚本与测试，不修改 SGLang 功能源码。ticket02 保持 open，需用户换新 run-id 重跑 NPU。

**2026-10-26 ticket02 重写：NPU 首轮 reference 误判修复。**

用户回传 ticket02-001 在 reference 阶段报 require zero-decode and at least two real decode requests，尚未进入 target。CPU 用真实 runner 与完整四请求日志复现同一异常；问题是旧检查器无条件要求 zero-decode，与 A/B/C/E 四个真实 decode 不符。

已让 prefix-acceptance 显式要求 0 个 zero-decode、4 个 completed；旧模式保留原要求。runner 回归及旧 service gate 共 10 项通过。负向回归、review 和推送仍待完成；本票 NPU 尚未验收。

**2026-10-26 ticket02 重写：两轴 review 整改与复核完成。**

Standards 两项可读性建议已处理：提取 SR admit 分支，新增布局及状态调用补参数名。验收 checker 的短页描述符问题经 red→green 修复。相关 42 项定向回归和 5 文件 strict mypy 通过。

Standards / Spec 最终均为 0 项剩余发现。功能仍为 9 个 Ascend/NPU 文件，无公共功能改动。交付说明列出功能阅读顺序和双机命令；准备独立提交，NPU 待用户执行，ticket02 保持 open。

**2026-10-26 ticket02 重写：完整 CPU 回归完成并进入两轴 review。**

完整 CPU 套件 437/437 通过，35.966 秒。
首跑缺 requests 测试依赖，补回已有缓存路径后重跑通过。
13 个源/脚本 strict mypy、21 个 Python 文件 lint/格式/isort/Python3.9 AST 检查通过。
验收 checker 拆分后负向定向测试通过。

已提供[交付说明](ticket02-delivery.md)与每机一条 prefix-acceptance 命令。
Standards/Spec review 进行中。
ticket02 保持 open，NPU 尚未验收，ticket03 继续等待。

**2026-10-26 ticket02 重写：自运行保存验收入口与负向证据检查。**

prefix-acceptance 已接入独立参考、A/B/C/E 冷算、逐 rank 原始/alive/prefix/释放后内容、实际源槽/目标目录关联及回执顺序检查。入口、证据与布局 18 项测试通过；补充 runtime/映射 22 项通过，包含坏 Index K 物理索引和最后区域映射不连续的拒绝。6 个相关源/脚本 strict mypy 通过。CPU 检查不代表 NPU 通过；尚待全回归、两轴 review 和独立提交。

**2026-10-26 ticket02 重写：启动接线与完整 service 生命周期。**

已接入实际模型启动、原生 Index K pool、PrefixStore 和 SR 注册；功能范围仍为 9 个 Ascend/NPU 文件。TP16 service 测试覆盖 warmup→native Index K 清空→延迟最后 rank 的 admit→alive free→prefix 留存，3 项 service 回归通过；4 个接线源文件 strict mypy 通过。NPU 启动与硬件内容验收仍待用户执行。

**2026-10-26 ticket02 重写：重复保存、容量与内容验证。**

CPU 路由与存储 18 项通过，覆盖 A/B/C/E、部分容量、非法回执和超时留存。新增验收模式参考链的 4 项存储测试通过：原始值→alive→prefix→alive 释放后留存，并验证 Index K 内容差异会报错。实际启动与验收 runner 尚待接线；本票 NPU 尚未验收。

**2026-10-26 ticket02 重写：第 4 步，Main 保存与释放路径通过 CPU 检查。**

Main 已保留 prefix 目录，新增独立页额度、源与父路径保护、admit ack 和先父后子发布。
11 项 Router 检查通过，含 native_freed 迟到、重复 ack 和 alive 释放后的 prefix 保留。
跨请求容量、完整 service 接线与内容验收仍待完成；NPU 尚未执行。

**2026-10-26 ticket02 重写：第 3 步，全 TP 异步 admit 通过 CPU 检查。**

SR 沿用两次 TP gather，统一分配 prefix 页；最后一个 rank 完成后才共同确认。
定向 9 项通过，覆盖队列一致、源保护、显式 free 和 service warmup。
首跑暴露重复命令缓存别名问题，修复后回归与两文件 strict mypy 通过。
Main 持久目录与启动接线仍待接入；NPU 尚未执行。

**2026-10-26 ticket02 重写：第 2 步，本地 prefix 保存接口通过。**

真实页接口已覆盖主 KV 与 Index K 的非连续页复制，设备事件完成前保护 alive 源。
CPU 保存测试通过；释放源并清空 alive 后，prefix 两类内容保持正确。
全 TP 协调、Main 目录和真实启动接线仍待接入；NPU 尚未执行。

**2026-10-26 ticket02 重写：第 1 步，Index K 写回接口通过 CPU 回归。**

新增三类 P 区域及带偏移映射，Index K 写回纳入 forward 完成事件。
定向 30 项 CPU 检查通过，覆盖 fake warmup、非连续原生索引和 native 释放后的留存。
下一步接入独立 prefix 页复制与全 TP admit；NPU 尚未执行，ticket02 保持 open。

**2026-10-26：ticket02 重写代码计划完成。**
用户授权重新实现，并要求先规划代码修改。
[实施计划](ticket-02-implementation-plan.html)列出 8 个现有功能文件和 1 个新增文件。
功能修改预计全部位于 Ascend/NPU，复用已验收的 warmup 与公共接线。

先补齐 Index K 写回完成，再接通异步 admit、publish 和显式 free。
SR 收齐 TP16 设备完成后，所有分片才共同确认并移除 admit 命令。
prefix 内容随跨请求目录保留，容量与 alive 独立。
P BM 贡献增至 13 GiB/rank，TP16 共 208 GiB；验证缓冲另计。

本轮仅核对代码与容量、编写计划，没有实施功能或执行新增 CPU／NPU 测试。
ticket02 保持 open，03／04 继续等待各自前置票验收。

**2026-10-26：ticket01 用户确认验收，后续三票规划复核完成。**
用户明确确认 ticket01 验收成功，01 已关闭，02 的前置条件已满足。
验收基线是 7275f29a05；旧 ticket02 实现继续撤回，本轮未开始重写。

核对结果：02 保存 → 03 恢复与后缀 → 04 完整验收，无需重新拆票。
warmup 修复保持真实请求生命周期，新增功能沿用其隔离规则。
fake warmup 使用原生临时资源，不建立 alive 绑定，不占 Router 额度。
启用 prefix 后也应保持目录为空，不产生 admit／publish。
真实请求缺少预留与 fake/real 混批仍须拒绝。

02 将保存 prompt 主 KV 和 Index K，并在 alive 释放后独立保留。
冷算 A／B／C／E 验证每份 prefix 为 32 → 40 → 40 → 40 页。
命中恢复仍属于 03，完整 V1–V10 仍属于 04。
详细约束见[任务索引](ticket-plan.md)与 [02 实施票](issues/02-prompt-prefix-save.md)。

本轮仅检查提交、代码与文档，未重跑 CPU 或 NPU 测试。
历史报告的 user_accepted=false 保留原样，当前确认记录为 true。
以下日期记录保留各次操作发生时的状态；当前状态以本段和进度表为准。

**2026-10-26 ticket01 手动 smoke 入口，第 3 步：用户 NPU 运行通过。**
三个入口文件已作为脚本仓库提交 42a1511 推送。
用户在 31 向 Main 发送铅笔分配问题，模型正确回答每人 6 支。
输入 128 token，输出 48 token，正常 EOS 结束，retractions=0。

本次确认独立 demo 的自然语言单请求正确性。
服务记录端到端耗时约 11.12 秒；这不是性能验收。
[回传结果](verification/glm52-mempool-demo-20261026/user-reported-response.json)保留回复及元信息。

D 启动配置开启 Graph；本次回复本身不含 replay 标记。
Graph 证据仍来自此前的自动化 NPU 验收。
没有修改功能代码；01 保持 open，等待用户确认关闭。

**2026-10-26 ticket01 手动 smoke 入口，第 2 步：本机检查与交付完成。**
两份 shell、内嵌 Python 和手动 curl 命令的语法检查通过。
P、D、Router 三组 dry-run 通过；D Graph BS=16 与 warmup 参数已核对。
7 个非法配置检查均按预期退出；Standards / Spec review 均无发现。

三个新增文件位于用户指定的脚本仓库；SGLang 功能代码未改动。
先启动 Main，再启动 P/D；三处 ready 后发送单条 greedy 请求。

客户端用 GLM chat template 编码，在 user 内容前补空白 token 对齐到 128。
默认问题为铅笔分配，预期答案 6；输入和回复保存在 /home/cryang/log/ 下。

手动入口尚未在 NPU 执行；这不改变已通过的自动化验收记录或 01 的 open 状态。

**2026-10-26 ticket01 手动 smoke 入口，第 1 步：新增启动脚本。**
用户要求在 ascend-sglang-script/pd-disaggregation 放置独立 demo 脚本。
新增 glm52mempool-demo.sh、router-mempool-demo.sh 和共享 JSON 配置。
worker 复用已验收的启动环境与参数；Main Router 单独启动。

默认使用 31/32，P eager、D Graph BS=16，原生 warmup 开启。
S_P=8192、S_D=128，DRAM/HBM page_size 均为 128。
本机三组 dry-run、容量和 Graph 参数检查已通过。
curl 使用文本转成的对齐 input_ids；本入口的 NPU 手动 smoke 尚待用户执行。

**2026-10-26 ticket01：31/32 自动化 NPU 验收首次通过。**
用户回传提交 7275f29a05 的两端报告，status 均为 passed。
运行编号为 ticket01-3132-20261025-04。
身份、fetch、reference 和 target 阶段全部通过。

4 个正式用例通过，P/D 各覆盖 16 rank，统一 page_size=128。
完整输出 token IDs 与同长度正式参考一致。
Graph、P/D BM 读取、显式释放、槽位复用和账本恢复检查通过。

故障注入触发预期的 SR heartbeat timeout。
D 的 16 rank 均保留预留资源，测试结束后两端进程清理通过。
[回传记录](verification/ticket01-3132-20261025-04/user-reported-results.json)保留原始报告字段。

本轮覆盖冷请求；prefix 未覆盖，物理 NUMA 页落点未验证。
自动化验收已通过，用户尚未确认关闭；01 保持 open。
02 旧实现已撤回，等待基于稳定的 01 重写。

**2026-10-25 ticket01 原生 warmup 适配，第 4 步：本机验证完成。**
共享测试 helper 整理后，完整 CPU 套件再次 421/421 通过。
Standards 和 Spec 均无剩余发现；静态检查及四组入口描述通过。
公共 HTTP 文件与重试补丁前逐字节一致。

本次独立提交接在 ticket01 后，不包含 ticket02 实现。
用户在 31/32 用新 run-id 重跑硬件验收；01 仍为 open。
02 旧提交已撤回，待 01 验收通过后重写。

**2026-10-25 ticket01 原生 warmup 适配，第 3 步：CPU 首跑与 review。**
完整 CPU 套件 421/421 通过，4 个功能文件 strict mypy 通过。
13 个改动 Python 文件通过仓库 lint、格式、isort 与 AST 检查。
Spec review 无发现；Standards 指出重复测试 loader，已共用并拆分 fixture。
共享测试 helper 修改后重新验证，NPU 尚未执行。

**2026-10-25 ticket01 原生 warmup 适配，第 2 步：定向 CPU 回归通过。**
真实 HTTP 回归分别验证 P/D：先等监听，再发本机 fake warmup，最后 freeze_gc。
本机回环测试明确绕过代理；公共 HTTP 实现无需连接重试。

原生 D 预分配、7 步 decode 和原生释放归还页及请求行。
同一池随后能预留真实请求；未预留真实请求和混批均被拒绝。
16-rank service warmup 后仍完成真实请求和显式释放。
完整 CPU 套件、双轴 review 和用户 NPU 验收仍待执行。

**2026-10-25 ticket01 原生 warmup 适配，第 1 步：最小路径改动已完成。**
fake warmup 先于 SR dispatch 检查识别，沿用原生分配与释放。
D 的三处既有 hook 仅对真实请求执行预留逻辑。
真实请求缺少预留仍失败，fake 与真实请求混批也失败。

参考组和目标组均启用 server warmup，并等待两端 ready。
fake batch 不计入真实 decode 步证据，不建立 alive 绑定。
公共 http_server.py 已恢复重试补丁之前的内容。
已复现 dispatch 误拒绝和 D 预留 KeyError；修复后回归待运行。

**2026-10-25 ticket01 原生 warmup 适配，第 0 步：撤回未验收的 ticket02。**
用户要求先稳定 ticket01，再重写 ticket02。
当前分支已移除旧 ticket02 提交及实现，保留恢复引用。
其他未提交修改保持原样；01 与 02 均为 open。

本次仅适配各机的原生 fake warmup，不跨 P/D 传请求数据。
临时 HBM 和请求行由原生 scheduler 分配与释放。
真实请求仍须经过 Main/SR；拟撤回公共 HTTP 重试修改。
本步仅完成代码基线整理，回归和 NPU 验收均待执行。

**2026-10-25 ticket01 第二次 NPU 验收：HTTP 启动竞态修复。**
31/32 在 93678e5fd4 通过身份检查，进入参考服务请求阶段。
P 日志显示 4096-token Prefill、DONE 和 free=16。
验收扫描到启动时的 freeze_gc ConnectionRefused，尚未进入目标组。

跳过 warmup 也跳过了监听等待，启动线程提前调用 freeze_gc。
真实本地 HTTP 回归已复现；现增加有时限的连接重试。
HTTP/TLS 错误和持续连接失败仍记录异常，验收器保持原有检查。

本次需修改公共 HTTP 启动函数，NPU hook 无法控制监听顺序。
本地 HTTP 回归 6 项、参考验收器 9 项通过，静态检查通过。
双轴 review 无阻断项；已补连接耗时计入预算的测试。
修复单独交付在 ticket01 之后；整票仍等待用户 NPU 验收。

**2026-10-25 ticket01 首次 NPU 验收：身份比较修复。**
31/32 已建立连接，复制与模型测试尚未启动。
本地 numa_nodes 为 tuple，经 JSON 传输变为 list，导致相同配置被误拒绝。

配置现先转成 JSON 表示；失败时也保存两侧身份和逐字段差异。
验收入口 CPU 10 项、静态检查通过；Standards / Spec 均无发现。
修复单独交付在 ticket01 基线上，等待用户在原目录重跑；01 保持 open。

**2026-10-24 统一页修正，第 1 步：实现已修改，验证待运行。**
Main 配置、页哈希、入口对齐、整槽账本和 SR 页描述统一从 `page_size=B` 推导。
NPU mempool 记录实际 B；注册前核对 worker、DRAM、allocator、KV 池和 Index K。
当前 GLM-5.2 mempool-min 配置默认并仅允许 128；不是所有 Ascend 后端的能力声明。

**第 2 步：已调整现有 CPU fixture 和自运行 NPU 入口。**
验收检查每 rank 的 DRAM/HBM 注册、整槽页表、额度与 native 预留。
启动从 PAGE_SIZE 传入统一 B；提前 EOS 输入按 B 对齐。

**第 3 步：已补充页大小负向和容量 CPU 测试。**
覆盖配置支持范围、P/D 任一 rank 不一致、实际 worker 组件不一致。
覆盖哈希分块参数、4096/5120 页数、整槽页表和验收报告拒绝旧口径。

**第 4 步：本地 spec、方案、目标页、回顾及 tickets 01–04 正文已同步。**
讨论与交接记录已追加新决定，旧 Comments 保留。

**第 5 步：完整 CPU 套件 416/416 通过（2026-10-24）。**
统一页负向配置、组件传递、容量、哈希参数、预留和释放检查通过。

**第 6 步：现行图解已重生成，原交互图保留为历史快照。**
小例和正式验收保持原 token 容量；代码已格式化。

**第 7 步：7 文件 strict mypy、13 文件 lint/AST 和启动 dry-run 已通过。**
现行决策索引和残留示例已复核；完整 CPU 416 项通过。

**第 8 步：图解公式检查及逐文件交付记录已完成。**
两套现行图解各通过6组公式、7种不支持B和容量对齐检查。
完整CPU日志与静态检查记录已归档，见[本轮改动与验证清单](unified-page-size-change.html)。

**第 9 步：syncfile设计源17个文件已写回并逐文件校验。**
完整设计、最小demo、讨论与交接、现行图解及历史快照均已同步。
dense检查点与跟踪间隔使用B，kernel chunk和缓存分块单独校验。
本轮实施与本地验证完成；剩余为用户NPU验收。

统一页修正按用户要求并入原ticket01提交；当前提交号以git log为准。

ticket01 保持 open；NPU 未执行，02–04 不解锁。
此前408项通过属于修正前记录；本轮完整CPU为416项。


依据：walkthrough 第 8 节，M19、M22。
原稿同时使用功能阶段和负载验收两套编号。
共享接入又单独记为 K1，放在负载与容量轨道中。
把它们直接串成路线图，会混淆开发内容和验收目标。

| 原文标记 | 表达什么 | 与实现的关系 |
|---|---|---|
| 阶段 0–4 | 功能逐步增加什么 | 基础请求流程、远端 prefix、流水、并发等 |
| L0–L3 | 验证哪种负载和容量 | 从单请求功能验收到长上下文并发验收 |
| K1 | 共享存储接入这项功能 | 安排在基础请求流程验收后、远端 prefix 前 |
| ticket01–04 | 当前发布的具体实施任务 | 合起来实现并验收基础请求流程 |

**L0 是阶段 0 的功能验收，不是另一个开发阶段。**
K1 是原文的共享接入标记，不代表“阶段 1”。
后续说明优先使用中文功能名，括号内保留原编号供回查。

已经明确的主推进顺序为：

```flow LR
基础请求流程并验收 -> 共享存储接入: 功能验收后接入共享
共享存储接入 -> 远端prefix与布局调整: 继续扩展
```

上述三项分别对应原阶段 0（含 L0 验收）、K1、阶段 1。
基础请求流程包括冷请求、prefix 保存与命中、生成、释放和复用。

| 功能名称（原文标记） | 当前本地承接 | 状态与边界 |
|---|---|---|
| 基础请求流程（阶段 0） | mempool-min 01 → 02 → 03 → 04 | 01 已关闭；02 待关闭确认；03 两轮 C 输出失败，待首层诊断；04 待实施 |
| 共享存储接入（K1） | 旧 NUMA 共享 02 有部分相关范围 | 尚无覆盖完整共享接入的新控制路径集成票 |
| 远端 prefix 与布局调整（阶段 1） | 本 feature 尚未拆票 | 包含 page-first |
| 分块与流水（阶段 2） | 本 feature 尚未拆票 | 包含逐 chunk 推进、提前首 token |
| 并发与动态容量（阶段 3） | 本 feature 尚未拆票 | 含 alive 页分配；并发与动态容量分步推进 |
| 生成缓存复用等扩展（阶段 4） | 本 feature 尚未拆票 | 包含 decode admit、混合页及副本重配 |
| dense 最小版 | 同事推进 baseline；本仓库集成未拆票 | baseline 交付仍需核对 |

**阶段 0 四票进度（2026-10-23 重新拆票）**

用户已确认详细回顾，并要求按该内容重新拆票。
四票已原位重编，保留编号、历史记录和直接依赖。

ticket01 已经用户确认 NPU 验收并关闭；02–04 保持 open。
现有 NPU 数据路径与共享组件证据不计为这四票完成。
逐票职责、数据流、验收与启动安排见[四票详细回顾](stage-0-ticket-review.html)。
执行索引见[ticket-plan.md](ticket-plan.md)，正式验收清单以独立票为准。

| Ticket | 已完成 | 待实现的主要行为 | 验证状态与进入条件 |
|---|---|---|---|
| [01 冷请求与显式释放](issues/01-cold-request-lifecycle.md) | 冷请求、统一页、原生 warmup；CPU、双轴 review 与用户 NPU 验收 | 本票已关闭 | 7275f29a05：自动化验收通过，2026-10-26 用户确认；user_accepted=true |
| [02 prompt 保存](issues/02-prompt-prefix-save.md) | Index K 写回、40 页 prefix、异步 admit/publish；用户回传双机 NPU 验收通过 | 功能与自动化验收完成；等待用户确认关闭 | 87157b7365、ticket02-002；CPU 440 项、两轴 review 与 NPU 报告通过；NUMA 落点未验证，State=open |
| [03 命中恢复与后缀](issues/03-prefix-restore-and-suffix.md) | 恢复、fill、后缀、提前释放已实现；首层诊断 CPU 464 项通过 | 用户运行 ticket03-003，随后完成 NPU 验收 | 两轮 C 输出失败；002 元数据与第 0 层 KV 相等，根因未知，State=open |
| [04 完整自运行验收](issues/04-full-demo-acceptance.md) | 规格、回顾确认及重编实施票 | 独立参考组、A/B/C/E、V1–V10 与报告整合 | 完整验收未执行；等待 03 关闭 |

每次实现后立即更新本表，分别记录代码、CPU 检查与用户 NPU 验收。
只有用户确认对应 NPU 验收后，才关闭该票并解锁下游。

负载验收单独列出，不插入功能阶段编号：

| 验收名称（原文标记） | 要证明什么 | 主要依赖 |
|---|---|---|
| 单请求功能验收（L0） | A/B/C/E、V1–V10；生成上限 128 | 基础请求流程 |
| 32K × 16 验收（L1） | 逐步增至真实并发 16，再验证 32K 输入 | 实际并发能力；不依赖动态容量 |
| 64K × 16 验收（L2） | 16 个真实请求、64K 输入 | 共享、实际并发及容量验证 |
| 128K × 16 验收（L3） | 16 个真实请求、128K 输入 | 共享、实际并发及容量验证 |

后三项尚未拆本 feature 的验收票，也未通过相应硬件验收。

旧票是 [ascend-mempool-numa-sharing/02：MLA 共享 KV 与写入并行](../ascend-mempool-numa-sharing/issues/02-mla-shared-kv-overlap.md)。
它与 mempool-min/02 的 prefix 保存是不同的票。
旧共享票覆盖共享主 KV、写入并行和真实服务证据。
其中 02-1 已实现配置规划，真实共享服务部分仍为 open。
生产 [启动检查](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py:255) 仍拒绝 share=1。
共享建池组件通过不代表模型服务已接通共享。

旧票保留 rank-pair 控制；新阶段 0 改由 Main/SR 接管。
后续 K1 必须适配新的分配和释放合同。
还须补齐 P alive Index K、prefix 的单份存储及广播。
因此，不能把旧共享 02 的范围直接等同于整个 K1。
其性能票 03 也不能替代这些功能接入工作。

中文名称用于说明已有范围，没有新增一套阶段编号。
后三类功能的具体依赖仍需拆票，不由上表行序推断。
D2 / D3 仍按原文保留为完整初版之后的可选扩展。

## E 本轮校正与保留的边界

本地规格、拆票索引和 ticket04 已明确标成 sparse 阶段 0。
它们的完成只证明 L0 和 V1–V10，不证明 K1 或后续目标完成。
ticket01 及 SR 讲解补充了私有副本和整槽后端的阶段限定。
后续目标及当前承接缺口集中记录在本页。

当前四票的功能与验收边界继续按阶段 0 执行。
K1 不是阶段 0 的前置条件；它也是明确的后续必接能力。
首次范围核对未发布新票；2026-10-26 已按用户确认关闭 01。

两项既有用户决定继续作为对 syncfile 的明确补充：

- NUMA 按当前拓扑连续均分；早期部分节点限制不再写死。
- dense 首模型改为 Kimi-Linear；容量须按实际模型重算。

dense 的 baseline 分工不会取消共享 prefix 与状态交接目标。
其范围与模型调整见 [实施讨论](discussion.md) 和 [会话交接](SESSION-START.md)。

## F 检查范围与证据

本轮对照了 syncfile 现行串讲稿、决策索引及本地规格。
同时核对了四张票、ticket01 方案、SR 讲解和旧共享票。
本次沿Main、SR、实际worker、DRAM配置、native预留和Index K传输调用链核对统一B。
CPU及静态检查证据见[本轮改动与验证清单](unified-page-size-change.html)。
文档和代码检查不能替代 NPU 验收。

实施参照：[阶段 0 规格](spec.md)、[拆票索引](ticket-plan.md)、
[ticket01 方案](ticket-01-implementation-plan.md)、
[SR 与 Scheduler](sr-scheduler-explained.md)。

本次按用户授权同步 syncfile 设计源、现行 HTML 与当前仓库。
