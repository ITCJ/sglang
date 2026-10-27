# 02: MLA 共享 KV 与写入并行

**What to build:** 真实 GLM-5.2 请求按各 rank 的 NUMA 分配及共享开关创建存储，
完成 prefill、handoff、Graph decode 与安全释放。默认每 NUMA 两个 ranks，
共享开启后每侧每组只有一份 compact KV；关闭后每 rank 一份。分工写入
尽量与 indexer、历史 KV 获取及同一步下一层 attention 重叠；当前 token
填入 selected buffer 与历史 UniDexCopy 并行，在消费者处汇合。写回在下一层
已有 TP 前完成发布依赖，末层收齐，避免新增跨 rank barrier。

**Parent:** [MLA NUMA 共享 spec](../spec.md)

**Blocked by:** None (can start immediately)。前置
[01](01-dual-rank-pool-benchmark.md)已于2026-10-14由用户接受并关闭。

**Status:** ready-for-agent

**State:** open

## 本次安排

按下面三个可验证步骤推进，仍归本票；全部完成并经用户 NPU 验收后才关闭02。

1. **先打通共享存储。** 实现 NUMA 分配→share 开关→分组→owner/映射，贯通
   真实请求的 acquire、prefill、handoff、Graph decode、drain/release。
   先用同层完成的保守依赖检查真实 KV、输出、池贡献与退出；双成员为主验收，
   单/四成员与不均匀分配通过同一分组合同，禁止写死两个成员或8组。
2. **再接入并行。** 当前 KV 本地填入 selected，和历史 copy 汇合；提前提交
   BM 分工写入，比较同层完成与覆盖下一层 attention。验证 Graph 内事件、
   staging 复用、最后一层收尾和普通 scheduler overlap。
3. **交付真实服务证据。** GLM-5.2 TP16 为主，GLM-5.1 快速回归；提供同负载
   初步性能、内容校验、timeline 和安全释放结果。每机一条自动命令、进度与
   分阶段报告；性能策略的正式3%门槛留给03，不能用01的纯 copy 结果代替。

## Scope and decisions

- 首版 TP16、DP1、PP1、CP1、BF16 MLA、非投机；GLM-5.2 为主，GLM-5.1 快速回归。
  每节点两个 ranks 是部署默认值；组由最终 NUMA 分配生成，两侧成员一致。
- 新共享部署模板显式设 `mempool_share=1`；未设置开关保持独立池兼容行为。
  `0` 每 rank 一池，`1` 同机同节点同逻辑 KV 合为一池。开关与分组在启动时
  固定；配置名、旧 NUMA 兼容和 owner 合同见[执行设计第0节](../execution-design.md)。
  同机只有一名成员时仍可工作，四名成员同节点时只建一份物理存储。
- 采用01通过的映射与写入候选。保留原独立池模式作端到端基线；共享模式比较
  producer stream 与独立 write stream、同层 join 与一层滞后。具体 DAG、对照与 buffer 合同见
  [执行设计](../execution-design.md)。
- compact KV 完成 norm/RoPE/必要重排后即可提前提交，不依赖 top-k。
  21 个 producer 层和 57 个 shared-topk 层分别测量；不能把没有 indexer
  窗口的层当成有相同重叠收益。默认调度由性能决定。
- 保留既有 rank-pair control、固定 slot 和全 TP 生命周期；共享身份参与握手。
  本票负责共享改动引入的 drain/取消/复用正确性，完整故障矩阵仍归原后续票。
- 首版不跨 decode step 积攒 pending writes；原 ticket04 的 P/D miss 双
  stream 优化仍后置。正常路径不用逐层 host/device synchronize。

## Acceptance criteria

- [ ] NUMA 分配与 share 开关独立；节点列表按本机rank连续均分（2026-10-22用户修订），支持逐 rank 显式
  分配。验证同机同节点、不同节点、不同主机同号节点、1/2/4成员及不均匀分配。
  输出所有 rank 的节点、group、owner、contribution 和 pool identity；重复/遗漏
  rank、未知/非法节点、P/D 成员不匹配、开关不一致或不兼容 layout/identity
  在共享分配/准入前明确失败。未启用共享仍保留独立模式，不静默合池。
- [ ] 一个 g 成员组每侧只有一个完整物理 owner，其余 g−1 名成员零贡献并独立
  映射；g=1/2/4复用生产 manager 的组件入口验证。单成员独立、双成员默认和
  四成员共享均报告实际贡献、完整读回和退出；不要求另重跑全部历史性能矩阵。
- [ ] 真实请求完成 acquire→prefill→handoff→Graph decode→drain→release；
  每侧每组只有一份 compact KV，所有成员都直接访问，各有效写入单元恰好写一次。
  每个 reader 获取完整 selected KV；分工处理余数、短于组大小及zero-writer，
  单 token 只有一名 writer 的候选如实报告。share=0 验证原完整副本路径。
- [ ] 写入提交从 backend 与本层 indexer 完成解耦；early enqueue 后原 hook
  不再重复写。source 打包、路由和实际 copy 均纳入性能对照与生命周期。
- [ ] 当前 token 按请求 logical position 从本地 KV 填入 selected KV，历史
  hit/miss 与 current destination masks 互斥且完整。未选中新 KV 仍持久化；
  padding、zero-valid、不同 P/D slots 不访问错误数据。current-fill 与历史
  UniDexCopy 独立并行提交，selected 零初始化先完成；公共 ready 不能包含
  current-fill 或 BM write-done，attention/refill 仅汇合所需 copy。
- [ ] 独立 writer stream 等本地 source/metadata，历史 fetch 与 attention 不
  等本步无关写入。write(L) 允许覆盖 attention(L+1)，TP(L+1) 前只接入
  write_done(L)，不用等待整个 writer stream；末层已有 TP 前收齐尾部。
  通信组覆盖共享组 writers，延迟 writer 测试证明后续历史读取安全。
- [ ] 实际 writer 覆盖、本地 current 可读范围、已发布历史范围分别验证；不能
  以本 rank 分片计数冒充完整 prefix，或以 CPU enqueue 计数冒充设备完成。
- [ ] 对比独立 current stream 与合入 hit stream 的 selected-ready 成本，
  明确后者的 current/historical HBM hits 顺序执行，不能声称三路全并行。
  独立 current stream 时 attention/refill 均纳入 current_done；slot-map、
  source、metadata/scratch 和 binding 复用均等待最后消费者。只增必要的持久
  writer stream，独立写入 scratch 不覆盖既有读/refill metadata。单执行
  stream 以两组 source/metadata 交替为最小候选；普通 scheduler overlap
  和连续异步 forward 保持正确，不能提前覆盖旧源或事件。
- [ ] Graph 捕获真实 fork/join，无 host `.item()` 分支；验证 producer/shared
  层、zero-writer、padding、重复 replay、batch graph 切换、slot/generation
  重绑。测试首层无前驱、中间层只等上一层、末层 flush；tail flush 必须在
  捕获模型内部且先于最后 TP，D completion snapshot 排在它之后。
- [ ] P 可并行推进 native HBM/计算/handoff 与 BM 写入的无依赖部分；pending
  source 有界，所有 prompt 分片实际完成后才发 KV_READY。D 同时等待 native
  readiness；P/D 的取消与 release 覆盖全部 writer/readers 和 handoff。
- [ ] 在实际量化、batch、Graph 配置验证组内 compact KV 数据等价及共享内容；
  GLM-5.2 短 greedy 与独立池对照，覆盖大于 index_topk 的实际稀疏读取；
  GLM-5.1 快速兼容回归通过。
- [ ] 分别交付 current-fill／历史读取串行与并行、同层 join／滞后一层的
  timeline 和耗时对照。报告 selected-ready、write(L) 与 attention(L+1)
  的重叠、下一层 TP 前剩余等待、tail flush 及同负载初步吞吐/ITL，
  覆盖小/大 batch、producer/shared 层。
  同时分析写与历史读争用、事件/打包成本；没有实际重叠或变慢时记录并调整。
  不能只以增加 stream 或正确生成文本完成本票。
- [ ] 自运行真实 PD 入口覆盖启动、readiness、请求、校验、报告与自身进程
  清理，每机一个命令，日志位于 `/home/cryang/log/`，失败返回非零。
- [ ] 关闭沿用01验证的全员 drain、pool close、客户端 uninitialize、Store owner
  最后退出顺序；进度明确区分请求完成和清理完成，有收尾超时及错误报告。
  源测试的修复不得在生产多成员接入时退回为并发无序关闭。
- [ ] 按[阶段交付流程](../../ascend-mempool/verification.md)交付实现说明和
  用户 NPU 验收证据，确认正确性、物理去重、并行路径与性能观察后关闭本票。

## Verification

复用既有真实 PD 服务验收入口，CPU 检查覆盖配置、身份、writer/readability 与
生命周期的外部合同。NPU 核对真实 KV 与 token 输出、Graph、共享读写的顺序和
资源释放。timing 与 profile 分开采样，保留原始数据。02 交付可用并行路径和
性能证据，最终策略选择与多负载 3% 正式验收由03完成。

NUMA 请求配置和实际页证据分别记录，无法观测时保留 unverified；用户接受01
此限制不替代本票的实际共享内容、贡献、Graph和生命周期验收。

## Comments

2026-10-12：按用户“设计更细致，最好能并行，不能影响性能”的要求建立。
允许首版评估独立 write stream，替代早期固定 producer stream 的限制；
本地 current 供给、既有 TP 发布边界和无额外 barrier 的决定保持有效。
尚未开始；须等待01用户硬件验收。验收后解锁[03](03-performance-acceptance.md)。

同日补充：用户明确 selected-fill 与历史 UniDexCopy 需并行，并确认“下一个
attn”为同一步下一层。采用一层滞后作为候选，保留同层 join 对照，末层
Graph 内收齐；当前仍为设计要求，没有实现或 NPU 结果。

2026-10-14：用户接受01完成，并确定默认每 NUMA 两个 TP ranks、按分配结果用
共享开关决定池数量。本票解除阻塞；更新为可变成员分组的真实服务接入，
保留原两处并行和验收范围。本轮为安排调整，生产代码尚未实现。

2026-10-14：按用户“计划如何实现”完成当前代码核对，形成
[实现切片计划](../ticket-02-implementation-plan.md)。按分组计划、生产共享池、
保守请求路径、selected并行、提前写回、真实服务证据推进，仍归本票。
明确current本地供给属于正确性首版；共享probe按成员分区；writer分片计数与
历史发布范围分开；生产关闭保留全员drain及Store owner最后退出。
本轮仅形成计划，未修改生产代码，未执行CPU功能测试或NPU测试；State保持open。

2026-10-14：按用户 `$implement` 实现02-1配置与NUMA分组计划。新增生产
`sharing_plan.py`，完成严格共享开关、旧列表轮转、显式host/rank/node映射、
1/2/4及不均匀分组、owner/participant、贡献/probe、端口及逻辑身份校验。
正式CLI注册map参数，但share=1或显式map在BM初始化前明确拒绝，组启动留到02-2。
Mac完整CPU suite 368/368、新自运行规划gate 34/34、严格类型及格式检查通过；
CLI验证正常预览与非法输入非零退出。规范/规格双轴审查问题已修复，复查剩余0/0。
交付细节、代码路径和主线改动必要性见[02-1总结](../ticket-02-1-summary.md)，
命令见[CPU gate说明](../../../ascend-mempool-test/NUMA_PLAN.md)。
未执行NPU测试；完整ServerArgs导入受本机缺少triton限制。父票State保持open，
实际共享池与服务路径待后续实现，仍等待用户执行NPU验收。
本次代码、测试及交付说明提交：`d328af12bd`（`cryang/dev/mempool`）。

### 2026-10-22：默认 NUMA 列表改为连续分配

用户核对 sharing plan 后要求：默认分配从轮转改为连续，例如 node0 给 rank0/1、
node2 给 rank2/3。该决定替代此前“保留旧列表轮转”的约束。

- 共享规划器和实际 `MempoolKVManager.create()` 共用 `contiguous_numa_node()`。
  按每台主机内 rank 升序连续均分；余数给列表前面的节点，节点多于 ranks 时只用
  前若干节点。TP16 配 8 个节点时每节点两人，配 4 个节点时每节点四人。
- 显式 rank map 不被均分覆盖；共享开关只影响是否合池。默认未指定节点时仍由
  SDK 分配，非法旧列表在独立模式下仍告警回退。共享/显式 map 的生产启动限制保留。
- 当前服务仍是每侧单机 TP16；直接创建池的组件可通过 `tp_size` 提供本机 rank 数。
  规划器在多机输入中分别按各主机的 rank 列表排序分配，没有开启多机服务支持。
- 偶数 NUMA 启动校验器同步检查连续映射；旧轮转日志不能作为这次新映射的证据。
  本改动只改变新建池的节点选择，不改变读写同步、ownership 或 drain/free 条件。
- 现行规格、运行说明和代码导读同步更新；旧 commit 的交付记录及实测历史保持原样。

Mac 实际执行：自运行入口
`/private/tmp/sglang-merge-venv/bin/python ascend-mempool-test/scripts/verify_numa_plan.py --self-test --output-dir /tmp/sglang-numa-contiguous-20261022`
通过 **48/48** 项 CPU 检查，报告和详细日志在上述输出目录。覆盖连续分组、余数、
节点多于 ranks、多主机、显式非相邻映射，以及实际 manager 传给模拟 SDK 的 flags。
报告包含 `npu_tested=false`；没有执行 NPU 测试，也不将 CPU 结果当成物理落点证据。
Ruff lint、格式检查和 `git diff --check` 通过；更新的代码导读及实现计划 HTML
渲染通过，STE 检查均为 0 条警告。

用户复验入口仍为 `ascend-mempool-test/BM_NUMA_DIAGNOSTIC.md` 的双机自运行
`run_bm_startup_gate.sh --even-numa`，每台机器一条命令，日志在 `/home/cryang/log/`。
本次每侧预期 rank0–3→node0、4–7→node2、8–11→node4、12–15→node6；该入口只验证
分配/映射和 probe，不代替真实服务验收。父票保持 open，等待后续实现及用户 NPU 验收。

### 2026-10-23：全节点容量测试交付

用户要求P 10.120.72.23、D 10.120.72.25，NUMA 0–7各96 GiB。
新增 --all-numa 固定配置，每rank48 GiB、每机768 GiB，沿用独立池和正常drain/close。
新检查器检查连续映射、HAL节点与大小、共同ready、probe及退出；旧even入口兼容。
CPU gate 51/51、lint/格式及shell语法通过；未执行NPU测试。详细记录见
[本次交付](../../mempool-min/numa-startup-verification.md)。父票保持open，硬件验收待补。
