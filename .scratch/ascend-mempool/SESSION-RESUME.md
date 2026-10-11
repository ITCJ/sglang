# Ascend mempool：新会话入口

更新日期：2026-10-11。用途：从长会话切换到新会话，继续开发。
本文件是进度快照；需求与验收以链接的 spec、ticket 最新记录为准。

## 先读这些

1. 仓库根目录 `AGENTS.md`、`CONTEXT.md`。
2. [阶段交付与 NPU 验收](verification.md)。Mac 做开发和适用的 CPU 检查，用户执行 NPU 验收。
3. [spec](spec.md)及[10：GLM-5.2 算法适配](issues/10-glm52-indexer-sharing.md)。
4. 仅在继续排障时读取[近期启动问题记录](startup-investigation-2026-10-11.md)。

## 当前工作区

- 记录时目录：`/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang`。
- 分支：`cryang/dev/mempool`；HEAD：`8717142d31b3afd01b619bffd08ba31f27212bf5`。
- 本轮记录前工作区干净。本轮只整理文档，未修改生产代码，未执行 NPU 测试。
- 新会话先执行 `git status --short --branch` 和 `git log -5 --oneline`，以实时结果为准。
- 邻接脚本库：`../ascend-sglang-script`；MF 研究库：`../gitcode/memfabric_hybrid`、`../gitcode/memcache`。
  `../memfabric_hybrid` 是另一个较旧 checkout，不能与 gitcode 版本或机器安装版混为一谈。

## 最新用户确认

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
| [10](issues/10-glm52-indexer-sharing.md) GLM-5.2 indexer sharing | open，可开始；已有不启用 offload 的单机短请求反馈 | 下一项，先审计已有支持及缺口 |
| [04](issues/04-single-request-graph-decode.md) 正式 Graph / 模型集成 | open，等待10；包含 P/D miss 并行读取建议 | 10后执行；并行具体方案仍需落实 |
| [05](issues/05-batch-slot-reuse.md) batch / slot复用 | open，等待04 | 多请求与复用系统验证 |
| [06](issues/06-acquire-wait-cancel.md) acquire等待 / decode前取消 | open，等待04 | 容量压力、超时、取消 |
| [07](issues/07-active-cancel-peer-failure.md) active取消 / 故障 | open，等待04 | drain、peer/rank故障、安全保留slot |
| [08](issues/08-accuracy-aime26.md) 精度 / AIME26 | open，等待05 | 固定token序列与数据集对照 |
| [09](issues/09-numa-allocation-followup.md) NUMA / 大容量 / SDK清理 | open、deferred、needs-info；BIOS进展已补记 | 用户独立跟进，不阻塞主线 |

执行依赖：01 → 02 → 03 → 10 → 04；04之后05/06/07，05之后08。
全部 feature 验收仍需各票条件满足，03通过不替代04–08或10。

## 继续 ticket10 的起点

用户模型路径 `/data_lib/data/models/glm-q`，模型身份按用户提供的 GLM-5.2 记录。
显式 `indexer_types` 是21个 full、57个 shared；78层 compact KV仍各自独立。
例如3/4/5层复用2层的 token 索引，但必须读取3/4/5层自己的KV。
完整 producer 列表、量化证据、源码入口和验收矩阵均在10，避免在此重复维护。

用户用 `glm52.sh` 跑过单机 P/D collocation、不启用 sparse KV offload 的短算术请求，
返回正确答案和 `finish_reason=stop`。响应 model 字符串仍为 `GLM-5.1-w4a8`，
不能以该字段识别真实权重；部署 SHA、实际权重和完整量化格式在后续 baseline 归档。
该结果是模型加载/短生成的初步证据，不证明 >2048 稀疏选择、普通 sparse PD 或 mempool 正确。

建议下一会话先核对代码已有的跳层/producer传递、Index K紧凑布局及PD层映射，
列出缺口与实施顺序，再按用户选择实施。关闭MTP，保持GLM-5.1兼容。
真实请求要覆盖长度小于、等于、大于2048，不能只复用03的context1024验收。

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

```text
$ask-matt 请先读取 .scratch/ascend-mempool/SESSION-RESUME.md，
再读取 ticket10、spec 和 verification.md，核对当前分支与工作区。
join 已解决；TransferEngine URL parser 失败在当前机器未复现，按记录封存。
我们继续 GLM-5.2 适配：先总结已有支持、待补缺口和下一步实施计划。
```

新会话读取后应能说明：下一票是10、已有短生成证据的边界、尚需NPU验收的内容、
两项启动问题的最新状态。达到这些条件即可继续，无需重读旧聊天或全部历史review。
