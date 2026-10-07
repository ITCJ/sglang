# Ticket03：把完整预检并入第一次 TP 同步

实现基线：`6eb6475f614a4f9523bdb4b883d904ef07c0eea6`；分支：
`cryang/dev/mempool`。用户于2026-10-07授权按本地准备、同步准备结果、共同规划后提交
的方向修改。属于S6.3后续优化，03仍为open，最终NPU复验待用户执行。

## 流程变化

| 阶段 | 修改前 | 修改后 |
| --- | --- | --- |
| 本地观察 | 活跃请求、服务事实、消息摘要 | 同样的观察，加每个候选动作的完整本地准备结果 |
| 第一次同步 | 交换观察后形成计划 | 交换观察、准备结果和记录容量，形成无资源冲突的计划 |
| 预检 | 复制全部control记录，模拟整批计划 | 同步前逐候选模拟，只复制该attempt及有界slot状态 |
| 第二次同步 | 汇总预检结果 | 汇总真实提交结果 |
| 第三次同步 | 汇总真实提交结果 | 已删除 |
| 发消息 | 所有rank提交成功后发送outbox | 相同；任一rank失败则全组fault，不发送outbox |

空tick也固定参加两次collective。没有增加空闲跳过分支，也没有把完整协议条件缩成
单个proof布尔值或`KVPoll`。网络线程仍只入队，scheduler线程拥有准备到提交期间的
协议状态；物理操作失败采用fail-stop，不承诺回滚已经完成的外部副作用。

## 修改位置与约束

- `disaggregation/ascend/mempool_control.py`：`prepare(identity, operation)`复用真实
  control状态迁移方法，隔离单个attempt、room归属、slot owner、generation和retirement。
  保留session、binding proof、阶段、容量与所有权检查。返回该动作是否需要新增record；
  `admission_capacity()`给出扣除活跃记录后的准入预算，终态记录仍可按原规则淘汰。
- `disaggregation/ascend/mempool_tick.py`：在第一次gather前准备可能被共同计划选中的
  消息和控制动作。每个候选携带校验错误或record claim。尚未成为共同动作的候选错误
  不提前终止tick；被选中时，任何rank失败都阻止整批提交。规划只读取共同观察，不读
  其他rank的proof/session。实际提交结果仍统一同步。
- `disaggregation/ascend/mempool_service.py`：只更新tick流程注释，服务集成入口不变。
- `test_pd_lifecycle.py` / `test_pd_tick.py`：沿用control生命周期和16-rank tick边界验证。

独立准备不能默认任意组合，所以同一tick只选择互不冲突的room和P/D slot动作，冲突
延后到下一tick。新record按全组最小容量预算准入，并在其他控制动作之后提交，避免
提前淘汰它们需要的历史记录；本tick释放的slot或record容量不提前计入预算。
`WAITING_RELEASE_ACK`继续占用活跃记录预算，即使其D slot已经释放。

新D请求在各rank最初可能有不同的attempt。仅对没有任何留存记录的fresh room准备
准入，最终统一使用leader的attempt。各候选用一个当前空闲slot检查准入，共同规划
核对全组空闲集合相同后分配不同slot。这里没有放宽旧attempt隔离或复用generation。

同时修复新D准入只看leader取消标记的问题：现在任一rank取消，都在全组分配前拒绝。
现有请求的取消也在所有rank准备；即使某rank尚未本地完成，它也为其他rank首先报告
release的情况准备释放候选，实际释放仍要求全组drained。

## CPU成本与边界

开发Mac、Python3.9.6，每项31组、每组20次tick取中位数。gather用本地观察重复16次
代替，不含真实跨进程通信、NPU或网络。准备期间保留ACQUIRED请求，不选择实际动作；
每组前后核对完整control快照不变。基线从上述commit导出，当前实现独立运行。
开发机脚本和输出：`/private/tmp/ticket03-prepare-bench/{run.py,before.json,after.json}`。

| 活跃数 | 终态历史数 | 修改前tick µs | 修改后tick µs | 修改前/后观察pickle字节 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 0 | 16.596 | 20.827 | 205 / 238 |
| 1 | 0 | 26.508 | 45.819 | 458 / 536 |
| 1 | 256 | 713.085 | 44.540 | 459 / 537 |
| 1 | 4094 | 11551.869 | 45.235 | 459 / 537 |
| 16 | 0 | 144.169 | 334.587 | 1374 / 1753 |
| 16 | 4080 | 11685.771 | 335.246 | 1375 / 1754 |

移除了随历史数量增长的全量复制，每tick的collective从3次减为2次。准备候选增加了
小历史负载的CPU工作和每活跃请求的少量同步字节，不能据此声称所有负载都变快。
表中history行多1字节是slot generation整数编码变长，不是同步了历史记录。
最终TPOT/吞吐变化必须以真实TP16服务复验为准。

## 本地验证与复查

TDD回归先观察旧实现空tick/准入tick仍需三次同步，以及非leader取消时仍准入的失败，
再修改实现。新增覆盖包括多请求合计record预算不足时等待、局部准备不改变live状态、
合法proof但阶段错误时拒绝、整批中后一个候选失败时前一个也不提交，以及真实副作用
失败时所有rank均禁止发送outbox。沿用旧/重复DONE、迟到ACK、历史淘汰、pending DONE、
取消和slot新generation复用场景。

最终独立CPU suite **205项通过**，其中TP tick文件15项通过。完整suite在最后的生产
修改后执行一次，日志为`/private/tmp/ticket03-prepare-unit.log`；故障注入的ERROR/FAIL
日志属于预期负向用例。严格mypy覆盖src/scripts、NPU runtime/row helper及PD
control/protocol/tick/service共33个源文件通过；standalone Ruff、生产文件仓库规则
`F401,F821,UP037`、53文件format及受影响文件isort检查通过。
isort首次误用standalone配置检查生产service文件，按生产仓库配置重跑通过，未改动其imports。

```bash
PYTHONPATH=ascend-mempool-test/src /private/tmp/ascend-mempool-s1/bin/python \
  -m unittest discover -s ascend-mempool-test/tests/unit -q
/private/tmp/ascend-mempool-s1/bin/mypy \
  --config-file ascend-mempool-test/pyproject.toml \
  ascend-mempool-test/src ascend-mempool-test/scripts \
  python/sglang/srt/hardware_backend/npu/mempool \
  python/sglang/srt/hardware_backend/npu/kv_rows.py \
  python/sglang/srt/disaggregation/ascend/mempool_protocol.py \
  python/sglang/srt/disaggregation/ascend/mempool_control.py \
  python/sglang/srt/disaggregation/ascend/mempool_tick.py \
  python/sglang/srt/disaggregation/ascend/mempool_service.py
```

复查以`6eb6475f61`后工作区diff为固定范围，按code-review技能并行进行两条审查。

### Standards

两项明确规范问题已修复并复核：新增`_Preparation`改用`msgspec.Struct(frozen=True)`；
每tick只构建一次`facts_by_room`供所有候选和真实提交复用，消除逐候选重复索引的平方级
工作。最终无遗留硬性违反。

保留一项非阻塞维护提示：本地候选和共同计划分别按role/phase选择动作。本地准备必须
覆盖可能由其他rank发起的动作，共同计划则按全组all/any事实决定是否执行，因此两者
不是相同谓词。实际状态迁移通过同一`_commit`和control方法执行；保留非leader取消、
不同初始attempt和迟到消息等回归约束，后续新增transition须同时覆盖候选和规划。

### Spec

无缺失、范围扩大或实现错误发现。复查覆盖候选动作集合、代表性slot与leader attempt
替换、共同record预算、admission末尾淘汰顺序、旧消息与复用slot、取消/drain/native
release组合及失败后的outbox限制。当前proof/phase/retirement/ownership校验均保留。

Standards：0项遗留硬性违反、1项非阻塞维护提示；Spec：0项发现。此次为增量复查，
不替代S6.4的全部01–03最终复查或S6.5的硬件验收。

## 用户NPU复验

两端更新到相同最终提交并重启服务，继续使用已验收的小容量配置：context1024、P/D
各512、TP16、D Graph width16、NUMA `0,2,4,6`。命令见
[正式服务验证](../../ascend-mempool-test/FORMAL_SERVICE.md)：组件fetch gate、
zero/decode/reuse请求、service checker、小题目输出及当前性能检查；普通模式按同页
命令回归。本轮没有执行NPU，不用旧日志替代新版本证据。NUMA/长上下文仍由09另行跟进。
