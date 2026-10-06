# Ticket03 S6.1：全量 code review

日期：2026-10-06。首次审查已完成；后续S6.2/S6.3已整改五项发现并复查增量，
最终全范围复查及NPU验收待执行，**03保持open**。

本文主体保留用户指定S6第1步时的审查结果、整改清单和验证要求；当时的生产代码、
测试断言及shadow删除随后在S6.2/S6.3实施。本报告不替代最终全范围复查和NPU复验。

2026-10-06后续处理决定：用户确认STD-01，指定STD-02按直接查询、活跃协议视图、
精简TP同步、单独评估preflight复制四步推进；STD-03改为删除调试统计，取消下文
原字段命名整理方案。两个Spec问题随S6.2迁移修正。具体删除边界、验证及顺序以
[S6计划](ticket-03-s6-plan.md#五项发现的处理决定2026-10-06)为准；下文保留首次
审查记录。S6.2已修正SPEC-01/02并删除shadow/旧READBACK，当前结果与复验边界见
[S6.2交付](ticket-03-s6.2-summary.md)。STD三项已在S6.3整改并对增量复查，
preflight复制策略单独评估后保留；详见[S6.3交付](ticket-03-s6.3-summary.md)。
最终01–03全范围复查及NPU验收待执行，以下首次发现原文保留。

## 固定范围与依据

| 项目 | 本轮记录 |
| --- | --- |
| 分支 | `cryang/dev/mempool` |
| 起点 | `4878a495d8bbacad52da8b75fd7b9685a32b8926`，首次独立mempool实现之前 |
| 审查目标 | `63590e9114e6d434291a3390bb103fbd26e3d1b9` |
| 固定diff | `git diff 4878a495d8bbacad52da8b75fd7b9685a32b8926...HEAD`；本轮审查期间HEAD未变 |
| 可重复比较 | `git diff 4878a495d8bbacad52da8b75fd7b9685a32b8926...63590e9114e6d434291a3390bb103fbd26e3d1b9` |
| 变更集合 | 30个提交、121个文件：33个生产文件、27个独立单测文件、2个registered测试、31个独立工具/支持代码/说明、28个spec/ticket/仓库文档 |
| 未提交输入 | 上轮已产生的`ticket-03-s6-plan.md`与ticket03规划更新；纳入本轮文档核对，没有未提交生产改动 |

范围包括`5a87606304`首次独立实现，不能从`a02bdc234e`的父提交起算。
`CONTEXT.md`的`295132c4a5`用于项目历史，不能替代本轮起点。

规范依据为[AGENTS.md](../../AGENTS.md)、[CONTEXT.md](../../CONTEXT.md)、
[阶段验收流程](verification.md)、`docs/agents/`约定及仓库pre-commit配置；
`docs/CONTRIBUTING.md`中的文档规则仅用于文档。
需求依据为[spec](spec.md)、[01](issues/01-mempool-kv-view-graph.md)、
[02](issues/02-rank-pair-control-lifecycle.md)、[03](issues/03-prefill-direct-offload.md)、
S1–S5交付记录、[S6计划](ticket-03-s6-plan.md)及用户最新确认。
历史文件中的阶段状态不覆盖最新ticket和用户决定。

Standards与Spec由两路独立审查分别输出，主审补充storage/runtime/Graph与共享资源入口
核对，并检查发现的实际代码依据。下列两组不合并计数、不跨组排序。
P2表示应在相应S6整改中处理，P3表示可读性整理；设计建议不等同于已经发生的运行错误。

## Standards

未确认仓库硬性规范违规；以下3项均为设计判断，属于review使用的code smell启发式。

### STD-01 · P2 · row推导重复维护（可能的Duplicated Code）

**位置：** `npu/mempool/rows.py:3–6,17–126`与
`npu/sparsity_driven_kv_offload/manager.py:721–905`，完整路径均在
`python/sglang/srt/hardware_backend/`下。

**证据与影响：** [rows.py](../../python/sglang/srt/hardware_backend/npu/mempool/rows.py)
直接要求“Keep both copies aligned”。正式BM与普通host路径各自推导decode、ragged、
static和MoE tail padding的request row、token position及validity；一次padding修正
需要同步修改两份实现。当前未发现两者已造成错误拷贝。

**整改：** 抽取共同的纯NPU坐标推导helper；地址构造、BM binding/容量mask和普通
SHM写入继续由各自所有者负责。普通路径不能因复用helper而引入BM初始化或SDK依赖。
空batch的调用方语义要显式保留。

**验证/复查：** 复用row/offload/writer的独立预期值用例，覆盖decode、ragged、static、
tail padding和空输入；随后跑普通host roundtrip与正式writer/fetch gate。
不能仅比较两份实现输出相等。**状态：未整改，归S6.3。**

### STD-02 · P2 · 单请求查询依赖完整历史快照（可能的Feature Envy）

**位置：** `disaggregation/ascend/mempool_service.py:306–320`、
`mempool_control.py:239–269,275–292`、`mempool_tick.py:121–145`。

**证据与影响：** service为找一个请求执行`control.snapshot().requests`全表扫描；
snapshot含最多4096条留存终态记录，tick又收集全表并在preflight复制记录。
活跃调度查询与用于重复消息ACK的历史留存因此耦合。工作量和collective负载会随
历史记录数增长；没有实测证据表明本项导致已验收负载的性能退化。

**整改：** 由control提供准确identity/room查询，分开活跃调度视图与终态ACK留存，
在一次tick内复用稳定观察结果。保留generation、retirement、pending DONE及重复
ACK证明；`WAITING_RELEASE_ACK`仍属于待完成协议进度。不得以减小快照为由删除
事务预检或破坏preflight/commit语义。

**验证/复查：** 生命周期与TP事务用例、slot复用后重复DONE、积累大量终态记录后
继续处理一个活跃请求；比较历史积累前后的快照大小和tick开销，再做NPU服务/性能
复验。**状态：未整改，归S6.3。**

### STD-03 · P3 · fetch字段依赖数字位置（可能的Mysterious Name / Primitive Obsession）

**位置：** `npu/mempool/runtime.py:534–546,599–602,742–774`。

**证据与影响：** 六列设备数据的生产、miss更新、完成检查和报告分别依赖
`[:, 4:]`、`sample[:3]`、`[3]/[4]/[5]`。修改字段时必须跨函数手工保持顺序，
也不容易区分正确性检查与来源统计。

**整改：** 集中定义命名字段索引，明确各列意义；保留每forward独立快照及事件完成
后才读取的约束。无需引入通用指标框架，也不能删除有效范围/覆盖检查。

**验证/复查：** 复用连续queued-forward、非法selection、padding及fetch_result计数
用例，不新增只镜像常量名称的测试。**状态：未整改，归S6.3。**

## Spec

确认2项P2，均为S5切换后未同步更新的验证代码。在本轮覆盖的生产路径中，未发现
其他可证实的正确性缺陷；这不构成全部并发、故障和硬件组合已验证的结论。

### SPEC-01 · P2 · 资源gate仍要求正式模式启动失败

**位置：** [verify_resources.py](../../ascend-mempool-test/scripts/verify_resources.py):213。

```python
if formal:
    assert pool.get_contiguous_buf_infos() == pool.get_state_buf_infos()
    require_rejected(mode.validate_runtime_support, "S5")
```

**需求依据：** ticket03:291–293要求验证正式模式旧host SHM/main-KV staging实际
分配为零及普通模式恢复；432–435要求整理checker并在最终版本重跑受影响gate；
461–462要求实际资源证据。

**触发与结果：** 任一正式模式在前置配置与NPU分配成功后都会走到上述断言。S5已允许默认
`READBACK=0`，`validate_runtime_support()`正常返回，于是gate误报
`AssertionError: Expected rejection containing 'S5'`。D侧之后的host/staging及
alloc/free/clear检查无法执行。S3历史通过记录仍有效，本问题是当前版本重跑失败。

**复现边界：** 在Mac直接调用原生产方法与原gate helper，两个正式mode均得到上述
AssertionError；没有模拟NPU分配，也没有声称整个NPU gate已在Mac执行。可复现入口：

```python
from ascend_sparse.config import SparseKVOffloadMode
from verify_resources import require_rejected

require_rejected(
    SparseKVOffloadMode.PD_DECODE_MEMPOOL.validate_runtime_support, "S5"
)
```

导入路径为`PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts`；将mode
换成`PD_PREFILL_MEMPOOL`得到同样结果。

**整改/验证：** 更新或移除过期阶段占位断言，使正式gate继续执行资源检查；若暂时
保留READBACK负向用例，应显式传入`readback_enabled=True`。S6.2删除shadow后按新
合同整理此用例。验证两个正式mode及保留的普通mode，新增能发现此分支漂移的回归，
不得用删除资源检查来获得通过。**状态：未整改，归S6.2验证入口迁移。**

### SPEC-02 · P2 · registered启动测试仍断言shadow模式

**位置：** [test_sparsity_driven_kv_offload_config.py](../../test/registered/unit/npu/test_sparsity_driven_kv_offload_config.py):221–230。

```python
for readback in ("0", "1"):
    # Set READBACK, construct runner, then:
    configure_for_model_runner(runner)
    self.assertIs(
        runner.sparse_kv_offload_mode,
        SparseKVOffloadMode.PD_DECODE_MEMPOOL_SHADOW,
    )
```

**需求依据：** ticket03:432–435、475要求测试对应最终正式实现并覆盖启动行为。

**触发与结果：** `MEMPOOL=1`在当前`config.py:60–61`返回正式D模式。
`READBACK=0`会使上述模式断言失败；`READBACK=1`会在启动校验提前抛ValueError，
也不会进入其期待的shadow分支。它已不能验证测试名所声明的“资源分配前校验”。

**证据边界：** 通过源码逐分支核对确认；本机没有完整SGLang运行环境，未执行整个
registered测试。它不在本轮通过的202项独立CPU suite中。

**整改/验证：** 按最终正式模式更新启动预期，迁移shadow矩阵时保留普通模式、非法
拓扑/容量在分配前拒绝等行为。除独立suite外，在完整SGLang环境运行该registered
文件和`test_hisparse_pool_configurator.py`。**状态：未整改，归S6.2覆盖迁移。**

## 覆盖范围与保留的约束

下表按调用边界核对完整开发增量。新核心模块按实现审阅，共享大文件按diff及相关
调用上下文审阅；历史Markdown用于确定决策/验收，并非逐段重新评审全部历史文字。
诊断探针按接口、范围和断言核对，09继续封存，不把硬件调查扩为本轮工作。

| 边界 | 审查的内容与结论 |
| --- | --- |
| 配置与资源 | capability矩阵、BF16/拓扑限制、启动先校验后分配、P native KV与D sparse/Index K分配、普通模式隔离；发现SPEC-01/02 |
| BM布局与映射 | KVLayout/PoolLayout、容量和UINT32边界、贡献大小与rank stride、首尾映射检查、raw地址视图、handle存活与关闭顺序 |
| writer与坐标 | decode/ragged/static/tail padding、zero-valid、每层写入和slot边界；发现STD-01 |
| fetch/cache | prompt与decode位置路由、可读长度、HBM hit/refill、无效top-k mask、slot map重置、两个copy节点和Graph固定元数据 |
| runtime与Graph | binding身份、submitted/completed、连续forward独立快照、capture无真实绑定、replay更新、hit/miss/refill/slot-map事件收尾；发现STD-03 |
| 协议/控制/tick | wire record严格结构、peer/session/attempt/generation、lease proof、双readiness、TP计划/预检/提交、迟到/重复消息与有界历史；发现STD-02 |
| native传输 | 现有KVArgs、Index K注册和布局、aux/state、empty-last、失败/在途cancel与drain；不新增AscendKVArgs |
| 服务及共享生命周期 | scheduler准入与batch准备、P native handoff、row detach和BM slot分别释放、D完成/取消、peer故障停止复用 |
| 验证实现 | 27个独立测试文件完整执行；核对writer/fetch/Graph/transfer/resource/service gate与AST隔离加载边界，另检查2个registered测试改动 |
| 文档及工具 | 当前运行说明、NUMA诊断工具、阶段记录和shadow引用盘点；过期阶段文字随S6.2/S6.4更新，历史通过记录保留 |

以下检查有独立正确性作用，不能按“无意义检查”批量删除：

| 约束 | 保留原因 |
| --- | --- |
| peer layout、Index K注册与传输长度 | 约束跨进程buffer解释；即使空末块仍需aux/state的完成与失败语义 |
| session/attempt/generation/retirement | 防止迟到ACK/DONE/CANCEL作用于新owner；历史记录缩减也必须保持 |
| row binding与P/D物理slot分离 | request row可先归还，P BM仍可能被D读取；两者释放条件不同 |
| 容量、实际已写长度、非法selection | 保证padding和未写位置不会被当成真实KV或污染cache |
| 每层完成与每forward独立快照 | 五次连续提交共用设备输入，不能把后一个forward的计数当成前一个完成证据 |
| drain后detach/free及联合readiness | Python调用返回不能证明NPU/远端读取完成；native Success也不能单独放行decode |

未将`runtime`的每forward clone/CPU读取直接判为冗余；它们承载独立完成证据。
如需优化，应先说明替代的完成/错误收集方式并验证连续异步提交。
`tick.py:286–288`的局部闭包在本次循环内即时调用，没有发现工具B023提示对应的行为错误。

## 后续清理边界与共享代码必要性

S6.2既定删除清单仍有效，留存shadow是已计划工作，不计为本轮新增Spec缺陷：

| 对象 | 处理 |
| --- | --- |
| sparse config中的两个SHADOW枚举/能力引用 | 删除，保留正式与普通mode |
| `mempool/readback.py`和runtime/attention/service旧host参考比较 | 删除专用实现、状态、报告与异常路径；正式fetch检查独立保留 |
| `srt/environ.py`的READBACK及启动脚本导出 | 删除定义/引用；外部glm51mempool脚本待进入该步骤时核对，保持原格式 |
| `verify_shadow_service.py`及专用测试 | 先迁移仍有价值的公共生命周期覆盖，再删除 |
| resource/registered模式矩阵 | 同步解决SPEC-01/02，保留普通路径行为验证 |
| `verify_writer.py`等已知pattern读回 | 保留；这里的远端数值读回不是旧host shadow参考模式 |
| README/READBACK_SERVICE及阶段文档 | 当前命令更新，历史票据和验收记录保留并明确历史用途 |

生产整改继续优先放在Ascend/NPU目录。已存在的共享入口有实际生命周期职责：

| 共享位置 | 必要职责/本轮建议 |
| --- | --- |
| `arg_groups/fields/disagg.py`、`environ.py` | 参数入口及环境定义；删除READBACK必须清理共享定义，仅改后端会遗留失效配置 |
| `mem_cache/kv_cache_configurator.py`、`model_executor/pool_configurator.py` | 传递已解析mode并按实际设备资源计容；保留普通路径回退 |
| `model_executor/model_runner.py` | pool分配前配置、backend后runtime初始化、Graph/eager forward边界；后端单独无法拥有这些时序 |
| `managers/scheduler.py` | request准入、batch准备和统一TP tick；不能移到kernel内部决定 |
| `disaggregation/prefill.py`、`decode.py` | native队列/页回收时机、handoff/cancel/drain；需要与原队列生命周期对接 |
| `scheduler_components/batch_result_processor.py` | 正常完成与取消后的回收入口，保持普通路径兼容 |
| `test/registered/unit/`两处测试 | 验证共享pool/runner真实构造接入；SPEC-02需要同步迁移，独立mock不能替代 |

没有依据要求为了目录偏好删除上述必要hooks，也没有建议新增主线通用框架。
保留现有KVArgs；IndexKTransferLayout继续表达peer的实际传输合同。

## 本轮实际验证

| 检查 | 结果与范围 |
| --- | --- |
| 独立CPU完整suite | **202 tests，全部通过**；27个测试文件，5.380秒，CPU PyTorch 2.8.0 / Python 3.9.6 |
| 严格mypy | **35个源文件通过**：独立src/scripts、NPU mempool、sparse config、四个PD control/service模块 |
| 仓库pre-commit Ruff规则 | **81个改动Python文件通过**，`F401,F821,UP037` |
| 语法 | 81个改动Python文件均由AST成功解析 |
| Ruff format | 独立工具、NPU mempool与四个PD control/service模块共67个文件通过 |
| 额外默认Ruff规则 | **检查未通过，共43条诊断**：UP045×24、BLE001×7、TRY004×4、UP035×3、B023×2、C408/SIM102/PLC0414各1；未修复，不计入人工Standards发现；仓库hook指定规则已单独通过 |
| SPEC-01原helper复现 | 两个正式mode均得到过期拒绝断言的AssertionError |
| 完整SGLang registered suite | 未执行；缺完整依赖环境，SPEC-02结论来自源码合同核对 |
| NPU及远端BM | 本轮未运行；已有S5结果来自用户此前反馈，不作为整改后硬件通过 |

主要复现命令（本机现有venv可替换成README中的开发venv）：

```bash
PYTHONPATH=ascend-mempool-test/src \
  /private/tmp/ascend-mempool-s1/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v

/private/tmp/ascend-mempool-s1/bin/mypy \
  --config-file ascend-mempool-test/pyproject.toml \
  ascend-mempool-test/src ascend-mempool-test/scripts \
  python/sglang/srt/hardware_backend/npu/mempool \
  python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py \
  python/sglang/srt/disaggregation/ascend/mempool_control.py \
  python/sglang/srt/disaggregation/ascend/mempool_protocol.py \
  python/sglang/srt/disaggregation/ascend/mempool_tick.py \
  python/sglang/srt/disaggregation/ascend/mempool_service.py
```

CPU完整输出保存在本机`/tmp/ticket03-s6-review-unit.log`；默认Ruff提示保存在
`/tmp/ticket03-s6-review-ruff.json`。这些是本次开发机辅助输出，不是远端验收资料。
202项通过不覆盖SPEC-01的NPU资源gate主流程或SPEC-02的registered文件，所以不
应被表述为“全部测试入口均通过”。

## 交付与复查状态

- [x] 固定01–03完整增量，完成Standards/Spec首次审查与主审核对。
- [x] 记录发现、影响、位置、整改方案、验证方法及共享入口必要性。
- [x] 运行适用本地检查并区分通过、工具提示及未执行项。
- [x] S6.2迁移覆盖并删除shadow，同时修正SPEC-01/02。
- [x] S6.3处理STD-01/02/03，逐项记录整改和增量复查结果。
- [ ] S6.4复查最终生产版本和整改diff，整理当前工具命令及静态检查口径。
- [ ] S6.5同版本重新通过正式S5、普通sparse PD及用户性能确认后关闭03。

S5按当前小容量已获用户输出/性能确认；09的NUMA/长上下文承载问题由用户独立
处理，不扩大本轮验收。04–08范围保持原定，未运行的压力/取消/故障矩阵及完整
数据集精度不宣称通过。

首次发现：Standards三项设计建议，最高P2；Spec两项P2验证代码缺陷。S6.2/S6.3已整改；
整改交付分别记录增量复查，最终全范围复查和NPU实测尚未完成。
