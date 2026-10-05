# Ticket03 S5：正式服务接通与验收交付

日期：2026-10-05。实施基线：`8e00b36cf980159f9228bdd1c3440adc0c51f4a4`。
需求见[S5计划](ticket-03-s5-plan.md)和[ticket03](issues/03-prefill-direct-offload.md)。

**代码已实现，等待用户执行NPU验收。** 正式模式可以进入服务初始化及Graph路径，
本地CPU和静态检查通过；真实server、curl小题目和性能尚未验收。
Ticket03保持open，S6尚未开始。性能由用户验收时查看实测指标并判断，不要求预先提供阈值。

## 本次行为变化

`SGLANG_NPU_ENABLE_MEMPOOL=1`现在选择正式P/D模式。
P保留prefill计算所需的native HBM KV，并把prompt KV写入P BM。
D保留HBM Index K及sparse cache，attention的miss从P/D BM读取。
S3的零旧host KV/staging分配和S4的Index K/aux传输由正式入口接通。
当前BM使用DRAM，不能把BM与D的HBM sparse cache混为一层存储。

`MEMPOOL=0`继续选择原sparse PD模式。正式模式在资源分配和BM初始化前拒绝
`MEMPOOL_READBACK=1`，因为旧host参考已停用。shadow专用代码留待S6删除。
本次没有新增独立的host、传输或fetch组合开关。

## 生产代码：5个文件，均在Ascend/NPU目录

以下路径相对`python/sglang/srt/`。

| 修改路径 | 修改内容及作用 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | MEMPOOL开关返回正式P/D enum；解除阶段占位拒绝，提前拒绝正式模式与READBACK组合；保留模型、拓扑、dtype等实际限制。 |
| `hardware_backend/npu/attention/ascend_backend.py` | attach时核对mode与runtime fetch能力一致，正式D必须启用fetch；保留维度、设备和row数核对。 |
| `hardware_backend/npu/mempool/runtime.py` | 在现有逐层检查中记录有效selected KV、P miss、D miss；完成事件后累计到binding，生成`fetch_report()`。报告拒绝fault、旧binding及未完成forward。更新decode长度改用设备`fill_`，避免该处的标量host拷贝。 |
| `disaggregation/ascend/mempool_service.py` | 记录实际已分配buffer和已注册地址的资源统计；drain完成后、detach/native free前输出正式`fetch_result`，携带cancelled/drained及完成计数。 |
| `disaggregation/ascend/conn.py` | Index K和aux成功发送后记录实际路径的逻辑字节数与`main_kv_bytes=0`，供服务验收核对。原worker及handoff控制继续复用。 |

没有新增生产文件，也没有修改ModelRunner、scheduler、公共KVArgs或Mooncake worker。
`copy.py`的两次UniDexCopy、manager/attention的hit/refill及NPU Graph hooks均复用已有实现。

## 正式数据链路

```text
P native prefill计算 -> P BM写入 -> KV_READY
P Index K / aux原生传输 -----------> native transfer完成
                                      |
                          两项均完成后D才可decode
                                      |
D本层compact KV -> D BM写入 -> 本层可读长度
Index K -> top-k -> HBM sparse cache lookup
  hit  -> HBM cache -------------------------+
  miss -> P BM copy -> D BM copy ------------+-> selected KV
                                                |       |
                                            attention  HBM refill
```

P/D copy始终进入Graph，mask决定各自需要复制的位置。P copy负责prompt范围，
D copy负责decode范围。同一层的两次copy串行，hit与miss由现有stream处理。
固定inputs地址与capture时的selected buffer保持有效，replay只更新设备内容。
capture和padding使用无效binding，不消耗真实slot。

现有事件覆盖顺序保持为：binding更新→copy_ready→hit/miss完成→attention/refill，
attention返回前等待refill及slot map更新，runtime随后快照并记录completion event。
`poll_completed()`只消费已完成的快照，再累计请求统计。
正常decode没有新增逐token的设备同步；结束时仍执行whole-D drain。

## 完成、释放与日志的含义

```text
停止提交 -> drain -> 收集完成事件 -> fetch_result
         -> row detach -> native free -> release / DONE
         -> P释放对应BM slot并回复ACK -> D CLOSED
```

| 证据 | 能说明什么 |
| --- | --- |
| `mempool resources` | 实际native/HBM sparse/host/staging/Index K字节数，以及注册表中是否仍有main KV。正式D的native main KV、旧host与staging均应为0。 |
| `mempool native_copy` | 当前正式发送路径成功提交的Index K/aux逻辑字节数；不是网卡或底层协议的总流量计数。 |
| `mempool fetch_result` | 对应binding实际完成的forward、Graph replay、逐层覆盖、selected/hit/P miss/D miss计数。只有完成后才输出，不宣称与参考KV做了数值对照。 |
| `row_detach`、`native_free`、`DONE/RELEASE_ACK`和`free=16` | native资源回收及P/D BM租约释放的先后顺序和最终可用slot数量。 |

`selected_kv = cache_hits + prompt_misses + decode_misses`，这些计数跨层累计。
`written_kv`按实际提交/完成的forward计数；overlap可能多提交一步，不能用响应
`completion_tokens - 1`硬推占用。zero-decode合法地报告0个forward。

新增设备计数及完成快照有运行成本；是否达到性能目标必须用固定负载测量。
这些完成证据不替代独立pattern校验，也不替代用户的模型回答检查。

## 测试与交付脚本：9个文件

以下路径相对`ascend-mempool-test/`。

| 状态 | 路径 | 功能 |
| --- | --- | --- |
| 修改 | `scripts/verify_fetch.py` | 每轮输入预先放到NPU，连续提交5个forward及设备快照，最后一次同步再核对；覆盖P/D miss、mixed、all-hit、zero-valid、padding、完成前禁止detach及重绑。保留30个case、block_dim 24/48和capture/eager/replay目标地址回归。 |
| 新建 | `scripts/run_service.sh` | 统一P/D启动，提供formal与native对照；保留context1024、P/D各512、TP16、D Graph bucket16小容量配置；支持dry-run，记录版本、参数、环境和日志。 |
| 新建 | `scripts/verify_service.py` | 读取真实P/D日志，检查全16 ranks资源、传输、Graph、attempt/lease、两项readiness、完成/释放顺序、zero-decode、两次以上decode、物理slot新generation复用和最终free16；不会自动宣布精度或性能通过。 |
| 修改 | `scripts/verify_pd_transfer.py` | 补充解包后request非空断言，区分消息对象与序列化frames变量，修复扩大mypy范围时发现的S4脚本类型问题。六个S4测试场景不变。 |
| 修改 | `src/ascend_sparse/fixture.py` | 更新fixture说明，避免继续把正式服务称为未开放模式。 |
| 修改 | `tests/unit/test_sparse_config.py` | 正式P/D选择、READBACK拒绝及MEMPOOL=0兼容矩阵。 |
| 修改 | `tests/unit/test_fetch.py` | 使用延迟事件验证多个待完成forward、完成后报告、旧binding拒绝，以及row/slot重绑后的计数归零。 |
| 修改 | `tests/unit/test_pd_service.py` | 正式zero-decode走drain、报告、native free及DONE，旧shadow数值报告不会出现。 |
| 新建 | `tests/unit/test_service_gate.py` | 验证完整日志可通过；缺rank、错误统计、缺少P detach/free、错误释放顺序、旧存储及fault必须失败；覆盖D联合readiness两种顺序，以及P native释放后再发布ready。 |

## 文档与票面

| 状态 | 路径 | 内容 |
| --- | --- | --- |
| 新建 | `ascend-mempool-test/FORMAL_SERVICE.md` | 双机组件gate、P/D/router启动、三个生命周期curl、小题目、日志checker、普通sparse PD性能对照及回传清单。 |
| 修改 | `ascend-mempool-test/README.md` | 指向正式服务交付；区分当前formal入口与旧shadow历史命令。 |
| 修改 | `python/sglang/srt/hardware_backend/npu/mempool/README.md` | 更新正式模式、数据源、完成/资源日志及S5待硬件验收状态。 |
| 修改 | `.scratch/ascend-mempool/issues/03-prefill-direct-offload.md` | S5代码交付记录及当前阶段状态；保留open和硬件验收条件。 |
| 新建/更新 | `.scratch/ascend-mempool/ticket-03-s5-plan.md` | 保存此前用户确认的S5范围，并记录本轮实现交付。 |
| 新建 | `.scratch/ascend-mempool/ticket-03-s5-summary.md` | 本文，代码路径、执行链路、验证事实及后续步骤。 |
| 纳入已有修改 | `.scratch/ascend-mempool/ticket-03-s4-plan.md`、`ticket-03-s4-summary.md` | 此前用户确认的S4六个case通过及负向场景解释，不属于新增S4代码需求。 |
| 纳入已有新文件 | `.scratch/ascend-mempool/ticket-03-s6-plan.md` | 此前用户确认的S6全量review、shadow删除及可读性清理计划；本次没有实施S6。 |

Ticket04未修改。

## Mac实际验证

- 独立CPU完整suite：**195项通过**。
  `PYTHONPATH=ascend-mempool-test/src python -m unittest discover -s ascend-mempool-test/tests/unit -q`。
- 期间按配置、fetch、service、transfer、服务checker分别执行定向测试；先复现新增合同
  缺口，再修改实现。已通过的S2–S4组件和生命周期测试仍包含在完整suite中。
- mypy：**34个源文件通过**，覆盖独立包/脚本、mempool runtime/config以及Ascend
  protocol/control/tick/service。使用`ascend-mempool-test/pyproject.toml`配置。
- 评审后补充P侧日志顺序回归，先复现5个漏检的失败subtest，再修复checker；
  `test_service_gate.py`定向6项全部通过。
- 13个修改/新增Python文件通过Ruff、isort及语法检查；shell语法、4种role/mode的
  启动dry-run、`git diff --check`和84个文档本地链接检查通过，代码围栏闭合。

测试环境：Mac，`/private/tmp/ascend-mempool-s1`虚拟环境，Python3.9.6。
完整CPU结果保存于`/private/tmp/ticket03-s5-unit.log`。
本机没有NPU；未运行真实Graph、远端BM copy、完整server、curl或性能测试。

## Standards

未发现阻塞性规范违反。生产修改局限于Ascend/NPU，保持open及用户NPU验收流程。
评审指出2处文档状态不一致：S6计划仍称S5待实施，S4总结未标明旧shadow启动是
历史状态；均已更正。1项非阻塞维护建议为给runtime统计列命名，归S6可读性清理。

## Spec

发现1项P2：服务checker原先只核对P侧事件存在，遗漏detach/free和关键顺序。
已补充`BOUND_ACK → start_prefill → row_detach → native_free → native_release → DONE`
检查；ready可在native_release前后，但必须位于start_prefill与DONE之间。
补充并通过针对性回归，规格评审已复核确认修复，无遗留规格问题。
其余未发现确定的生产正确性问题或范围扩张。

规范评审：0项阻塞、2处文档更正、1项S6维护建议；规格评审：1项P2已修复并复核。

## 下一步

按[正式服务验收说明](../../ascend-mempool-test/FORMAL_SERVICE.md)依次执行：

1. 双机连续异步fetch组件gate，双方应`ALL_CHECKS_PASSED`。
2. 启动正式P/D与router，运行zero-decode、两次真实decode及日志checker。
3. 用户curl发送答案为33的小题目，核对回答和对应非零Graph完成/释放证据。
4. 固定负载下测量3轮，用户查看TTFT/TPOT/吞吐并判断；普通sparse PD作为诊断对照。
5. 回传环境、日志、JSON结果和人工结论；全部通过后记录S5验收并解锁S6。

按照[阶段流程](verification.md)“等待用户执行NPU测试”，当前代码交付不等于S5关闭。
