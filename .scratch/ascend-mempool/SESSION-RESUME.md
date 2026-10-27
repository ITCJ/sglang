# Ascend mempool：新会话入口

更新日期：2026-10-14。用途：从长会话切换到新会话，继续开发。
本文件是进度快照；需求与验收以链接的 spec、ticket 最新记录为准。

## 先读这些

1. 仓库根目录 `AGENTS.md`、`CONTEXT.md`。
2. [阶段交付与 NPU 验收](verification.md)。Mac 做开发和适用的 CPU 检查，用户执行 NPU 验收。
3. 当前优先 [MLA NUMA 共享规格](../ascend-mempool-numa-sharing/spec.md)、
   [交付计划](../ascend-mempool-numa-sharing/delivery-plan.md)与
   [双 rank 对照和并行设计](../ascend-mempool-numa-sharing/execution-design.md)。
   [共享01](../ascend-mempool-numa-sharing/issues/01-dual-rank-pool-benchmark.md)已由用户接受关闭，
   当前从[共享02](../ascend-mempool-numa-sharing/issues/02-mla-shared-kv-overlap.md)
   接入真实服务；[设计证据](../ascend-mempool-numa-sharing/design.md)供查阅。
   旧合同见 [spec](spec.md) 与[10的关闭证据](issues/10-glm52-indexer-sharing.md)。
4. 仅在继续排障时读取[近期启动问题记录](startup-investigation-2026-10-11.md)。

## 当前工作区

- 记录时目录：`/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang`。
- 分支：`cryang/dev/mempool`；紧凑Index K实现为`8705f5de45`，已推送。
- 最新交付新增容器自动验收入口，版本以实时git log为准；agent未执行NPU测试。
- 新会话先执行 `git status --short --branch` 和 `git log -5 --oneline`，以实时结果为准。
- 邻接脚本库：`../ascend-sglang-script`；MF 研究库：`../gitcode/memfabric_hybrid`、`../gitcode/memcache`。
  `../memfabric_hybrid` 是另一个较旧 checkout，不能与 gitcode 版本或机器安装版混为一谈。

## 最新用户确认

- 2026-10-14：用户审阅TP1/2/4 C/D结果后明确“ticket01可以视作完成”。共享01
  已关闭，02解除阻塞，03仍等待02。默认每NUMA两个TP ranks；先分配NUMA，
  share=1同机同节点同逻辑KV合为一池，share=0每rank独立。组大小按实际分配
  生成，不写死两个成员；本轮已更新规格与票据，生产接入尚未实现。
  cd-v1-resume2两侧493/493并正常收尾；354项CPU回归通过，最新交付d2c999b4fa。
  物理页NUMA仍unverified，原始结果不改写；用户接受此限制后推进02。
  后续入口以[共享SESSION-START](../ascend-mempool-numa-sharing/SESSION-START.md)为准。
- 下列2026-10-12及更早条目为历史决定，当时的待实施/待验收状态已由上条更新。
- 2026-10-12 设计阶段收束：用户通过 `ask-matt` 明确“ok，我觉得初步设计完成了”。
  规格与三票为已确认的初步设计基线，后续实施入口为共享01的双 rank 读写对照。
  无需重复设计确认；实现及NPU验收尚未开始，不能关闭implementation tickets。
- 2026-10-12：优先实现 MLA NUMA 共享，再衔接04。P/D 组成员一致、NUMA各自
  配置，首验每组2 ranks；当前步KV本地供给，历史KV共享，复用既有TP同步。
  同负载吞吐、ITL中位数和P95回退均不超过3%；不硬顶3M或2T容量上限。
  用户核对三阶段后要求01先做独立/共享池双 rank 同时写与完整读的性能对照，
  02细化并行路径，03无异议。规格与三票已按反馈发布；尚未实现或执行共享NPU测试。
  最新确认：current KV填入selected与历史UniDexCopy独立并行；write(L)允许
  覆盖同一步attention(L+1)，TP(L+1)前只等write_done(L)，末层已有TP前收齐。
  保留同层join对照，不跨token、不新增TP barrier；交替source/metadata受完成
  依赖保护，tail flush必须在Graph内。01组件与读写性能验收后才解锁02。
- 2026-10-11：**join 问题已解决**。会话未给出最终根因、修复步骤及对应版本，记录为用户确认解决。
- 同日：**TransferEngine 的 URL parser 失败在用户目前可访问的机器未复现**。
  记录为当前环境未复现，不标记为已确认代码修复或已确定根因。
- 两项均不作为当前继续开发的阻塞，不主动重开排障；再次出现时使用独立问题记录恢复。
- 用户此前联系管理员更新 BIOS，并反馈 NUMA 探测约 `2045.88GB`。
  原反馈单位照录；缺少原始报告，不能等同实际同时分配该容量成功，详见09最新补记。

## Tickets 快照

`Status: ready-for-agent` 是 triage 标签；完成与否看 `State` 和验收记录。

| Ticket | State / 当前结论 | 后续 |
| --- | --- | --- |
| [01](issues/01-mempool-kv-view-graph.md) KV view / Graph | closed，双机组件验收通过 | 保留回归 |
| [02](issues/02-rank-pair-control-lifecycle.md) 16 rank-pair 控制闭环 | closed，历史 shadow/读回/正常生命周期通过 | shadow 已由03删除，旧记录仅作历史 |
| [03](issues/03-prefill-direct-offload.md) 正式路径 cutover | closed，用户确认 S6 NPU 验收完成，无剩余整改项 | 不重开 S6 |
| [10](issues/10-glm52-indexer-sharing.md) GLM-5.2 indexer sharing | closed，用户确认两模型验收及剩余核对项无问题 | 保留回归证据 |
| [NUMA共享](../ascend-mempool-numa-sharing/delivery-plan.md) | feature open；共享01 closed，02可开始，03等02 | 当前优先：可配置共享接入与并行 → 3%验收 |
| [04](issues/04-single-request-graph-decode.md) 正式 Graph / 模型集成 | open，03/10均关闭；用户要求先完成NUMA共享 | 共享之后；P/D miss并行建议保留 |
| [05](issues/05-batch-slot-reuse.md) batch / slot复用 | open，等待04 | 多请求与复用系统验证 |
| [06](issues/06-acquire-wait-cancel.md) acquire等待 / decode前取消 | open，等待04 | 容量压力、超时、取消 |
| [07](issues/07-active-cancel-peer-failure.md) active取消 / 故障 | open，等待04 | drain、peer/rank故障、安全保留slot |
| [08](issues/08-accuracy-aime26.md) 精度 / AIME26 | open，等待05 | 固定token序列与数据集对照 |
| [09](issues/09-numa-allocation-followup.md) NUMA / 大容量 / SDK清理 | open、deferred、needs-info；BIOS进展已补记 | 用户独立跟进，不阻塞主线 |

执行顺序：01 → 02 → 03 → 10 → NUMA共享 → 04；04之后05/06/07，05之后08。
共享内部顺序为01 → 02 → 03，每票经用户NPU验收关闭后才解锁下游；原04显式等待共享03。
全部 feature 验收仍需各票条件满足，03通过不替代04–08或10。

## ticket10已关闭，优先NUMA共享

当前启动脚本使用 `/data_lib/data/models/GLM-5.2-w4a8`；最初配置来自`glm-q`目录。
模型身份与producer布局依据实际config核对。
显式 `indexer_types` 是21个 full、57个 shared；78层 compact KV仍各自独立。
例如3/4/5层复用2层的 token 索引，但必须读取3/4/5层自己的KV。
完整 producer 列表、量化证据、源码入口和验收矩阵均在10，避免在此重复维护。

用户用 `glm52.sh` 跑过单机 P/D collocation、不启用 sparse KV offload 的短算术请求，
返回正确答案和 `finish_reason=stop`。响应 model 字符串仍为 `GLM-5.1-w4a8`，
不能以该字段识别真实权重；部署 SHA、实际权重和完整量化格式在后续 baseline 归档。
该结果是模型加载/短生成的初步证据，不证明 >2048 稀疏选择、普通 sparse PD 或 mempool 正确。

紧凑21层Index K的容量估算、分配与普通/正式Ascend PD接线已完成，78层compact KV
继续独立。用户已回传glm52-r2两端控制台：组件、四组模型、token对照全部通过；
详细P/D summary及两组formal service.json已收到并核对，实机SHA为`4dc8607ef3`。
此结果为用户实际NPU执行证据。GLM-5.1已按用户简化范围完成glm51-quick-r2双机
快速冒烟，修复BatchEncoding准备错误后通过。2026-10-12用户确认剩余核对项无问题，
明确要求关闭10，已完成关闭并解锁04。两模型无需重跑；历史回归入口见[容器自动验收说明](../../ascend-mempool-test/TICKET10_DOCKER.md)在P/D各运行一次
`run_ticket10.sh --suite glm51-quick`，只启动一次GLM-5.1正式Graph，自动执行8个短请求
及答案/78层布局/生命周期检查；无需重跑GLM-5.2。日志统一在`/home/cryang/log/`，
保持MTP关闭。以后测试都交付自主运行入口，约定已写入根AGENTS.md。

## 已接受的工程边界

- 正式模式：P保留native HBM KV并写P BM，D写D BM；D attention从HBM cache及P/D BM取KV。
  Index K及辅助handoff继续走原通道，正式模式移除旧main-KV transfer和D长期host/staging。
- shadow、旧READBACK与旧debug统计已清理；未来命中/miss统计可独立建测试类。
- control直接查询、活跃/终态记录分离已经实现；完整本地准备与proof结果并入第一次TP同步，
  不再独立做一轮preflight同步。保留ownership、generation、retirement、binding和安全释放检查。
- P/D miss目的地址互斥支持并行设计，但metadata、写入就绪、scratch复用、Graph和drain仍需依赖。
  并行建议在04，尚未实施；P端HBM/BM双写并行不在该建议范围。

03的关键历史提交：`6eb6475f61`（查询/活跃视图/TP摘要）、`e838860ed0`
（本地prepare并入TP gather）、`90bcb1d805`/`2de91045d9`（最终整改）。
最终交付目标`4baa6038d5`，用户整体验收关闭03；未收到逐项最新NPU原始日志或部署SHA。

03短负载性能历史：128输入/64输出，64请求，并发1，warmup3，三轮均64成功。
三轮均值指标的中位数：TTFT 910.199ms、TPOT 129.747ms、输出吞吐7.044token/s。
只说明该负载观测结果，不代表长上下文/并发性能，也不提供未经测量的前后加速比例。

## 新会话可粘贴的第一条消息

NUMA共享的集中入口及决定覆盖表见
[SESSION-START](../ascend-mempool-numa-sharing/SESSION-START.md)。
其中包含具体实施命令；本地设计与tickets尚未commit/push，换checkout前需携带。

```text
$ask-matt 请先读取 .scratch/ascend-mempool/SESSION-RESUME.md，
再读取NUMA共享的spec.md、delivery-plan.md、execution-design.md及02 ticket，以及
.scratch/ascend-mempool/verification.md，
核对当前分支、工作区及02实施范围；共享01已由我接受关闭。
join 已解决；TransferEngine URL parser 失败在当前机器未复现，按记录封存。
ticket10已由我确认关闭；默认每NUMA双rank，用share开关控制合池。
先完成共享02/03，再衔接ticket04。
```

新会话读取后应能说明：10已关闭、当前优先NUMA共享、已有实机证据的边界、自主验收入口、
两项启动问题的最新状态。共享01已关闭，从02可配置共享及真实模型并行接入衔接，
无需重读旧聊天或全部历史review。
