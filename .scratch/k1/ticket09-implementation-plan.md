---
title: Ticket09：四组配置完整验收计划
subtitle: 保留原有验收思路，覆盖64/128页大小与128/512的D槽容量
template: doc
theme: blueprint
lang: zh
date: 2026-11-02
status: 仅计划；未修改实现，未运行CPU或NPU验收
---

本计划落实用户指定的四组组合。每组均执行原 ticket09 的完整功能验收。
依据：[ticket09](issues/09-k1-06-full-acceptance.md)、[执行文本第13节](k1-tickets.md:327)、[本地范围约定](spec.md)。
代码核对基点为 `f9be1c5989`。06、08、12、11均已关闭；09保持 `ready-for-agent / open`。

本轮只写计划。实施与NPU结果分别记录，用户确认后才能关闭09。

## A 四组矩阵与固定条件

`B` 是统一 token 页大小；`S_D` 是每个D槽的token容量。
P/D的DRAM、HBM、Index K、原生分配器及Router必须使用同一个B。

| 组合 | B | S_D | D每槽页数 | P每槽页数 | prefix页数 / 每段页数 | 长生成上限 |
|---|---:|---:|---:|---:|---:|---:|
| b128-sd128 | 128 | 128 | 1 | 64 | 40 / 5 | 128 |
| b128-sd512 | 128 | 512 | 4 | 64 | 40 / 5 | 512 |
| b64-sd128 | 64 | 128 | 2 | 128 | 80 / 10 | 128 |
| b64-sd512 | 64 | 512 | 8 | 128 | 80 / 10 | 512 |

固定 `S_P=8192`、P/D各16槽、TP16、DP1、单请求串行。
五类区域副本为 `8 / 8 / 1 / 1 / 1`；prefix八段、interleave开启、split_read按段。
使用page first与ticket11的 `pool_copy=true`；沿用两机已部署的SDK修复。

普通验收不增加请求级P alive/prefix/fill目标读回，内容沿用源摘要与正常恢复HBM比较。

保持prefix容量为5120 tokens。因此B=64时配置80页，每段10页。
这保持原A/B/C/E的容量压力和淘汰条件，不把缓存容量同时减半。

保留A/C/E输入4096、B输入5120；提前结束用例仍用128-token输入。
128能同时按64和128对齐，也避免64-token输入装不下模型聊天模板。
ABCE逻辑题输出上限仍128；长生成单独设 `G_max=S_D`、`ignore_eos=true`。

S_D=128时沿用context_length=8448；S_D=512时至少改为8832。
两者覆盖 `S_P + S_D + 2`，保留Scheduler与worker的两位置余量。
max_total_tokens继续至少覆盖context_length；现有16384可满足此条件。
预算依据实际布局计算，不把预算当作实测RSS或NUMA驻留量。

## B 当前代码缺口

底层几何可配置，不代表当前完整服务已放行四组配置。

| 已核实位置 | 当前行为 | 本票计划 |
|---|---|---|
| `mempool_router_protocol.py` | `SUPPORTED_PAGE_SIZES=(128,)`；固定S_P=8192、S_D=128 | 保持S_P；明确放行本次B={64,128}、S_D={128,512}；沿用一致性和容量校验 |
| `run_mempool_min.py` | 验收强制40页；生成越界用129；旧full与共享精简模式分开 | 参数化几何；生成越界用G_max+1；组合完整验收与K1证据 |
| `mempool_logical_cases.py` | 拒绝B≠128；长生成固定128 | 保留逻辑题，参数化首个不同页、长生成及报告描述 |
| `verify_mempool_prefix.py` / `verify_mempool_reuse.py` | 固定32/40页、3968命中、128步长和分段容量 | 按有效B、prefix容量及请求长度推导期望 |
| `verify_mempool_full.py` / `verify_mempool_min.py` | 固定65/64容量故障及127–128步长生成 | 按当前配置和实际forward合同检查，保留缺证据即不通过 |
| `run_pool_copy_acceptance.py` | 固定target/restart、除128与固定页字节 | 抽取复用检查；按阶段、几何和实际页列表核算 |
| `verify_mempool_decode.py` | 只读ticket06历史证据；当前worker已删除哨兵 | 恢复验收专用D证据，适配page first及跨页 |

B=64的模型内核、Index K、Graph和实际SDK映射尚未完成硬件验证。
代码中的128还可能表示head维度、量化分组或kernel tile；只修改token页相关常量。
若实机暴露B=64内核限制，记录具体限制并做最小适配，不能将用例改回128冒充通过。

## C 每组完整验收与期望值

原V1–V10保留，并合入05/06/07/08/12/11的证据。
四组分别生成报告；不以第一组通过代表另外三组。

| 项目 | 每组判据 |
|---|---|
| V1 命中 | A/B/C/E命中 `0 / 4096 / (4096-B) / 0` |
| V2 内容 | 主KV与Index K独立源摘要；正常恢复HBM比较；fill/admit覆盖与完成顺序 |
| V3 答案 | 同组reference与target独立标准答案；reference冷A/C稳定性；IDs差异单列 |
| V4 数据来源 | P-only、D-only、mixed读取；D读取对方写者数据；跨页历史行读取 |
| V5 释放顺序 | native释放、finished、读者停止及free_ack因果关系 |
| V6 额度 | alive整槽预留与归还；prefix占用由4096/B增长至5120/B并保留 |
| V7 部分命中 | C恢复4096/B−1页，重算最后B tokens；提前EOS独立验收 |
| V8 拒绝 | 四例入口拒绝；独立真实worker容量不足注册故障 |
| V9 复用 | 释放后槽位/代次复用；Graph更新绑定后不读到旧槽内容 |
| V10 单请求 | 每个真实decode step的resident/executing均为1 |

B=128时，C命中3968、恢复31页，prefix占用32→40。
B=64时，C命中4032、恢复63页，prefix占用64→80。
不额外补token来伪造命中；使用实际分词IDs与页哈希核对。

四类入口拒绝保持：输入不对齐、prompt超S_P、生成上限超S_D、不支持的text入口。
例如生成越界分别为129和513；拒绝前后账本、目录和完成记录不变。
独立容量故障继续让D rank15少报一页，真实分配值另存。
所需页数 `(S_P+S_D)/B` 在四组分别为65、68、130、136。
该测试验证真实注册路径的拒绝，不能用CPU重放替代。

长生成必须实际输出G_max，不能因EOS提前结束后仍标为长生成通过。
output token数与D forward数分别记录：首token由P产生时通常只有G_max−1次D forward。
因此不能声称“输出512即写入512行D KV”；按实际forward检查有效行。

多页组逐个核对 `k×B−1 / k×B / k×B+1` 的有效位置与最后有效D行。
若稀疏选择未自然读取某个边界，用独立fetch用例强制选择历史页边界。
报告区分真实模型选择和受控fetch，二者共同提供覆盖，不混写证据来源。

## D K1组合证据与原有边界

**注册与布局：** 32参与者、物理贡献者、映射者、写者分别计数。
核对8/8/1/1/1、prefix八段、区域字节、设备访问范围及page-first签名。
签名包括B与几何；页块接口用独立CPU oracle核对行地址、跨页/槽/段和宽地址。
实际NUMA采样随报告保留；无权限时标unverified，不用逻辑注册代替物理驻留。

**写覆盖与顺序：** 保留P分写、D特征分写、Index K writer及prefix每页单写者检查。
D逐步哨兵与延迟写者负测试在ticket06清理后已不存在。
计划从 `9b760a1add` 复用最小验收实现，按当前page-first接口重接。
仅在ticket09显式验收开关下启用；默认生产配置关闭。

哨兵比较上一D token整行与本rank完整HBM源，覆盖所有真实step和层。
Graph探针必须“关闭等待出现预期不一致、打开等待一致”。
负测试的预期失败由检查器识别，不能吞掉其他异常。

这是为原ticket09顺序条款恢复的D验收读回；P侧目标全量读回边界不变。
恢复方案属于本计划，尚未实际修改或启用。

**读字节与搬运：** load_back只计实际有效读取，all_gather padding另计。
定义 `M=L_main×B×row_main`、`I=L_index×B×row_index`。
N页admit为2N块、N×(M+I)字节；fill为9N块、N×(8M+I)字节。
load_back为N×(M+I)有效字节。计数按实际页列表和参与分片核对。

B减半时同样4096 tokens的整页命中字节不变，块数翻倍。
C的命中token数改变，其字节应随实际命中计算。
空分片仍参加汇总；库完成先于receipt，admit再经ack/publish释放源。

复用11的提交前50ms延迟和tick轮询检查，放在target同次请求内。
标明这是提交前延迟，不能写成真实DMA在途故障或性能数据。

**性能与历史限制：** 遵守spec中的测试分支约定。
性能基线并列、分段变差解释、三项遗留重报在baselinetest另行安排。
三项是A/E时间差、C fill长尾、每请求forward步数；功能报告仍记录实际步数。

物理驻留与性能不设本分支关闭门槛，但报告必须给出状态和依据。
真实SDK在途故障/超时、生产目标独立全量读回仍单列未验证。
服务超时不能替代SDK在途超时；旧ticket的接受边界不能自动豁免本轮逻辑答案失败。
真实多请求并发仍属阶段3a。

## E 自运行安排与精简原则

提供一个ticket09矩阵入口，复用现有双机Peer、Processes、就绪与清理流程。
每机一条命令，双方使用同一run-id，自动依次执行矩阵四组。
每组先保存effective-config、源码版本、模型/SDK/驱动/CANN与拓扑快照。
不同组重建池与Graph，检查双方配置身份；不得热改页大小。

```flow LR
配置及组件检查 -> reference: 六个用例与冷A/C稳定性
reference -> target: 六个用例与四类入口拒绝
target -> capacity: 独立注册容量故障
capacity -> timeout: 实际D预留后的服务超时
timeout -> 组合报告: 双端清理及端口核验
组合报告 -> 下一配置: 本组完成后推进
```

保持旧full的四个不同服务阶段。每阶段每组跑一次；四组共16次成对服务启动。
这比08/11的两次精简入口多，是恢复reference与真实容量故障所需的成本。

不再套一层target/restart六用例重复轮；已有四阶段同端口启动用于清理核验。
复用tokenizer与已准备的题目；独立组件检查放在组内服务启动前，不再额外加载模型。

重复测试最多2轮：首次执行，失败修正后仅补失败组合/受影响检查。
计划不自动安排第三轮，也不把四个不同配置当作相同用例重复四次。

沿用SGLang父进程清理worker，不增加逐rank释放回执或通用shutdown协议。
任一组失败保存报告、通知对端并返回非零；未运行组合显示not-run。
失败重测用新run-id，保留旧失败证据；只有同一最终版本的完整四组证据才能汇总通过。
实现若在运行中改变共同功能代码，需明确先前结果的失效范围。

日志建议为 `/home/cryang/log/mempool-min/<run-id>/<组合>/<role>/`。
各组保留原始日志、注册、请求/响应、内容比较、哨兵、页边界、字节及清理报告。
顶层matrix报告索引四组V1–V10与K1证据，区分passed/failed/not-run/unverified。
`user_accepted`始终由自动入口写false，等用户确认再记录接受结果。
实际CLI和逐机命令随实现交付；本文不把候选命令当作现有可执行入口。

## F 实施步骤与完成条件

| 步骤 | 工作 | 完成条件 |
|---|---|---|
| 1 配置与几何 | 放行四组；参数化prefix、context、额度和签名 | 四组describe正确；CPU独立oracle覆盖四组跨页/槽/段 |
| 2 题目和检查器 | 按B、S_D替换页相关常量；保持原逻辑题 | 128基线期望不变；64期望正确；错页、缺证据、错字节均被拒绝 |
| 3 D顺序与多页 | 恢复验收开关下的哨兵/Graph探针，补fetch边界 | CPU接口回归通过；NPU探针和服务证据可由同一入口收集 |
| 4 完整矩阵 | 复用full四阶段，组合K1检查及双端协调 | dry-run覆盖四组与全部阶段；失败非零且保留not-run项 |
| 5 交付与评审 | 合并关键CPU检查，Standards/Spec两轴review，写运行说明 | 说明包含文件阅读顺序、配置、判据与证据边界；尽量整票一个commit |
| 6 用户NPU验收 | 用户每机一条命令，回传两侧结果 | 四组完整证据合核；用户确认后关闭09 |

CPU测试只增加配置矩阵、独立地址oracle、证据拒绝及编排的关键覆盖。
优先扩展既有测试，不复制四套实现断言，不重复初始化模型。
每个实施步骤完成后更新目标页D并重生成HTML，同步ticket Comments和memory。
本轮文档检查不等于步骤1或任何CPU/NPU验收已完成。

## G 预计文件与阅读顺序

以下为影响分析，最终交付按实际净改动收敛，不要求逐文件修改。

Ascend/NPU功能源码（前缀 `python/sglang/srt/`）：

| 顺序 | 文件 | 用途 |
|---|---|---|
| 1 | `disaggregation/ascend/mempool_router_protocol.py` | 放行组合及验收开关，保留统一页与容量约束 |
| 2 | `hardware_backend/npu/mempool/layout.py`、`manager.py` | 审核多页与分段视图，验证实际地址和块大小 |
| 3 | `hardware_backend/npu/mempool/decode_write.py`、`shared_probe.py` | 复用历史D哨兵和Graph顺序探针，适配当前接口 |
| 4 | `hardware_backend/npu/mempool/runtime.py` | 验收开关装配、逐步证据及跨页绑定；保留正常执行语义 |
| 5 | `disaggregation/ascend/mempool_router_transport.py` | 注册/测试证据接线；现有容量故障公式已经参数化 |

通用主线功能源码预计净改动为0。若64页内核确需修改通用源码，实施前单独列出文件、必要性、hook不足与行为影响。

脚本、测试与文档另列：

| 文件 | 用途 |
|---|---|
| `ascend-mempool-test/scripts/run_mempool_min.py` | 复用完整阶段与参数化配置 |
| 同目录 `run_pool_copy_acceptance.py` | 复用搬运检查，解除阶段/页大小常量 |
| 同目录拟新增 `run_k1_full_acceptance.py` | 轻量四组编排、配置生成与总报告；复用已有进程和Peer逻辑 |
| 同目录 `mempool_logical_cases.py` | ABCE保持语义；参数化长生成和页边界 |
| 同目录 `verify_mempool_{prefix,reuse,min,full,shared,decode}.py` | 内容、地址、K1合核及完整报告；按实际修改范围交付 |
| 同目录 `verify_fetch.py`、`verify_page_first_copy.py` | 独立跨页选择与copy/Graph证据 |
| `ascend-mempool-test/tests/unit/` 相关既有测试 | 四组几何、证据负例、编排与接口回归 |
| 拟新增 `ascend-mempool-test/K1_TICKET09.md` | 每机一条命令、运行前提、预期结果与回传清单 |

## H 当前状态

已完成：读取最新依赖与验收边界，核实生产入口及脚本限制，写出四组完整计划。
待完成：上述全部实现步骤、CPU检查、review、NPU执行与用户确认。
四组当前均为not-run。没有因本次规划修改代码、运行硬件或关闭ticket。
