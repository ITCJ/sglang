---
template: doc
theme: blueprint
lang: zh
title: Ticket03 S6 最终全量 Code Review
subtitle: 4878a495d8 → e838860ed0 · 2026-10-07 · Standards 与 Spec 分别审查
scope: 36 个提交 / 123 个文件
---

## 审查结论

本轮完成最终目标版本的全量审查。发现两项 P2，尚未整改。未发现新的、可确认的正式主路径正确性缺陷。

| 审查轴 | 结果 | 本轴最高严重度 |
| --- | --- | --- |
| Standards | 1 项明确规范不符；另保留 1 项非阻塞维护提示 | P2：新增容器仍使用 dataclass |
| Spec | 1 项清理要求未完成 | P2：shadow staging 分支和测试残留 |

```callout warn 审查完成，Ticket03 继续 open
先处理两项 P2，再复查受影响代码。最终硬件验收仍有待项。
本轮只更新报告和进度文档，没有修改生产代码或测试实现。
```

本地 205 项 CPU 测试通过。用户回传的正式服务 checker 和三轮性能测量也通过各自检查。
这些结果不能消除清理缺口，也不能替代最终 fetch 和普通模式的 NPU 回归。

新发现使用 `STD-F01`、`SPEC-F01` 编号，与首次审查的五项发现区分。

## Standards

### STD-F01 · P2 · 新增容器尚未遵循 msgspec.Struct 规范

**依据：** [贡献指南](../../docs/docs/developer_guide/contribution_guide.mdx)第 175 行要求：

> Use `msgspec.Struct` for new data containers instead of `dataclasses.dataclass` or `attrs`.

该规则在基线 `4878a495d8` 中已存在。未找到适用豁免。
完整增量仍新增 26 个生产 dataclass，分布在 9 个文件。standalone 另有 2 个。

下表路径均相对 `python/sglang/srt/`。行号固定于本次目标版本。

| 文件 | 数量 | 代表位置与用途 |
| --- | ---: | --- |
| `disaggregation/ascend/mempool_protocol.py` | 6 | L49、L221、L237：布局、请求身份、slot lease |
| `disaggregation/ascend/mempool_control.py` | 3 | L31、L58、L73：快照、可变请求记录 |
| `disaggregation/ascend/mempool_tick.py` | 5 | L19、L39、L82、L93、L109：观察与动作 |
| `disaggregation/ascend/mempool_service.py` | 1 | L27：服务请求记录 |
| `hardware_backend/npu/mempool/runtime.py` | 6 | L41–97：绑定身份、写入与完成记录 |
| `hardware_backend/npu/mempool/config.py` | 1 | L15：配置 |
| `hardware_backend/npu/mempool/layout.py` | 2 | L24、L91：KV 与 pool 布局 |
| `hardware_backend/npu/mempool/manager.py` | 1 | L529：KV view |
| `hardware_backend/npu/mempool/copy.py` | 1 | L16：copy 索引 |

standalone 位置为 `ascend-mempool-test/src/ascend_mempool/verification.py:49` 和 `writer_cases.py:35`。

**影响：** 最新提交只迁移了 `_Preparation`。完整开发增量仍不符合容器规范。
没有证据表明这些 dataclass 导致已测负载退化，也未发现由此造成的数据错误。

**处理：** 统一迁移新增容器，逐类保留冻结、哈希、排序和可变记录语义。
尤其保留 `KVRowBinding(eq=False)` 的对象身份语义。
同时核对 `replace`、序列化、构造后校验和隔离复制的调用。

**验证：** 运行协议 roundtrip、proof、prepare 隔离、生命周期和现有 CPU suite。
迁移后检查正式服务及受影响的 NPU gate。

### 非阻塞维护提示 · P3 · 共同规划函数仍偏长

`mempool_tick.py:486` 的 `_plan` 约 216 行。
它同时处理消息优先级、清理、P/D 生命周期和新准入。
贡献指南第 173 行建议函数约 100 行以内，并让编排函数保持高层表达。

可按规划职责抽取 helper。候选准备位于 `:302`，其条件与共同规划并不相同。
本地候选需覆盖其他 rank 可能触发的动作；共同计划使用全组事实。
因此不能直接合并两套判断。当前未发现行为错误，本提示不新增独立整改计数。

## Spec

### SPEC-F01 · P2 · shadow staging 专用分支和测试仍残留

**依据：** [S6 计划](ticket-03-s6-plan.md)的“删除全部 shadow 专用代码”要求：

> shadow 专用脚本、fixtures 和测试代码：删除；仍有价值的公共行为覆盖先迁移到正式/普通入口。

**位置：**

- [conn.py](../../python/sglang/srt/disaggregation/ascend/conn.py)：L792–821 的 `update_status()`，L869–875 的 `abort()`。
- [sparse_pd.py](../../python/sglang/srt/disaggregation/ascend/sparse_pd.py)：L208 的可选 `release` 参数。
- [test_native_release.py](../../ascend-mempool-test/tests/unit/test_native_release.py)：L87、L103 构造组合；L114、L128、L140、L146 验证相关行为。

这些代码仍处理以下组合：

```python
sparse_pd_decode_staging is not None
mempool_control is not None
```

实际执行当前六种 mode 的能力矩阵后，确认该组合不可达。
模式能力定义在 `sparsity_driven_kv_offload/config.py:79–86`。
staging 构造器还在 `sparse_pd.py:56–60` 拒绝其他模式。

| 模式 | staging | mempool BM | 当前组合能否成立 |
| --- | --- | --- | --- |
| 普通 `PD_DECODE_OFFLOAD` | 有 | 无 | no |
| 正式 `PD_DECODE_MEMPOOL` | 无 | 有 | no |
| 正式 `PD_PREFILL_MEMPOOL` | 无 | 有 | no |
| 其他模式 | 无 | 无 | no |

**例子：** 单测手动设置 `mempool_control = object()`，再挂一个 staging pool。
随后断言 abort 后 staging 保留到 drained clear。
测试可以通过，但真实配置已经无法创建这一组合。

**影响：** S6 的 shadow 清理尚未完成。
这部分测试仍证明旧组合，不能作为正式 Index K 目的缓冲区安全释放的覆盖。
当前没有证据说明它导致已验收的正式请求出错。

**处理：** 删除互斥组合的分支和仅服务该组合的参数。
把有效 abort、ACK、drain 覆盖迁移到正式路径。
普通 staging 回归继续保留。

```callout warn 保留正式 native abort 协议
保留 `_send_abort_notification()`、`register_deferred_abort_room()` 和 ABORT_ACK 跟踪。
正式路径仍有 Index K 等目的缓冲区，必须等待远端写入结束才能释放。
```

**验证：** 分别使用“普通模式有 staging”和“正式模式无 staging”的 fixture。
覆盖显式 abort、poll 失败、ACK 只登记一次、延后释放及普通模式即时清理。
之后执行普通模式实际 NPU 回归。

## 固定范围与已有整改

| 项目 | 固定值 |
| --- | --- |
| 分支 | `cryang/dev/mempool` |
| 基线 | `4878a495d8bbacad52da8b75fd7b9685a32b8926` |
| 目标 | `e838860ed0e4bfef540087d5cdd5ebbd1bcee992` |
| Diff | `git diff 4878a495d8...e838860ed0` |
| 增量 | 36 个提交，123 个文件，30,061 行新增，456 行删除 |
| 工作区 | 审查开始时无 tracked 修改；3 份已有未跟踪 HTML 未纳入代码范围 |

该范围包含 01–03 的实现和共享接入点，也包含测试、脚本及文档。
`CONTEXT.md` 的 `295132c4a5` 是项目历史基线，用途不同。
本报告及进度更新产生于审查之后，不属于上述固定 diff。

Standards 与 Spec 由两个独立 reviewer 并行审查。
主审另核对 Graph、runtime、共享 hooks 和证据边界，并复核两项发现。

| 首次发现 | 当前代码结果 | 对应提交 |
| --- | --- | --- |
| STD-01：row 推导重复 | ok 两条 writer 共用 `kv_rows.py`；各自保留存储约束 | `9ba89e46eb` |
| STD-02：查询及 tick 依赖全量历史 | ok 直接查询、活跃视图、精简观察；准备只隔离相关记录及有界 slot | `6eb6475f61`、`e838860ed0` |
| STD-03：fetch 调试统计 | ok 删除统计及 checker 依赖；保留正确性与完成检查 | `25bfbddfa3` |
| SPEC-01：资源 gate 期待正式模式拒绝 | ok 改为正式资源行为及普通模式回归 | `912012f3d0` |
| SPEC-02：启动测试期待 shadow | ok 改为正式模式预期，保留非法配置检查 | `912012f3d0` |

这些是代码复查结论。它们不代表所有最终 NPU gate 已完成。
`SPEC-F01` 是此次完整复查发现的清理遗漏，不能由旧五项整改完成推导为已解决。

## 路径与安全合同复核

下表记录已审查的关键合同。未发现确认缺陷不等于覆盖了所有硬件失效场景。

| 模块范围 | 本轮核对内容 | 结论或边界 |
| --- | --- | --- |
| 配置、资源、pool configurator | opt-in；正式 P 保留 native HBM；正式 D 停用旧主 KV；普通路径保留 | 静态与 CPU 检查通过；完整 registered / NPU 构造链仍待验收 |
| BM layout、manager、copy、writer | P/D 容量、layer base、映射、row/slot 区分、来源路由、invalid/padding | 未发现新确认缺陷；最终远端内容由 fetch gate 验证 |
| runtime、Graph、cache、attention | 固定 metadata、两路 copy 入图、写入及 miss 完成、cache refill/reset | 代码保持事件依赖；未在 Mac 执行 NPU kernel |
| protocol、control、tick | attempt/session/generation、proof、retirement、准入预算、历史淘汰 | 已核对旧消息和 slot 复用；两轮同步仍保留逐 rank 校验 |
| service、Index K 传输 | matching KV_READY 与 native Success 联合就绪；只发布必要 Index K/aux | 正式 checker 已由用户回传通过 |
| scheduler、prefill/decode、result hooks | deferred free、取消、zero-decode、idle tick、native handoff、drain | 未发现新确认缺陷；shadow staging 残留单列 SPEC-F01 |
| 测试与工具 | fetch/resources/service gate、模式矩阵、fixture、启动脚本 | 发现旧 fixture 残留；工具问题见下一节 |

### e838860ed0 的两轮 TP 同步

```flow
本地稳定观察 -> 候选完整准备: 校验 proof 与状态迁移
候选完整准备 -> 第一次 TP 同步: 观察及准备结果
第一次 TP 同步 -> 共同计划: 全组条件与冲突约束
共同计划 -> 本地提交: 仅提交共同选择的动作
本地提交 -> 第二次 TP 同步: 提交结果
第二次 TP 同步 -> 发送 outbox: 全组提交成功
```

候选准备复用 control 的真实迁移规则。
共同计划排除 room、P slot、D slot 冲突，并使用全组最小记录预算。
已选候选准备失败时，不提交整批动作。
真实提交失败时，全组进入 fault，并禁止发送 outbox。

`WAITING_RELEASE_ACK` 即使已经释放本地 slot，也继续参加协议推进。
迟到和重复消息仍验证旧 attempt，不能释放新 owner 的 slot。
P 原生 row 的释放与 P BM slot 的释放继续分开。
D 完成生成后仍须 drain；P slot 等待 D 的 DONE。

## 本地验证与工具问题

以下检查由 agent 在开发 Mac 上实际执行，目标为 `e838860ed0`。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 独立 CPU unit suite | ok 205 项，5.768 秒 | 生命周期、TP 事务、资源、writer/fetch、Graph 元数据等 |
| mypy | ok 33 个源文件 | standalone src/scripts、mempool、kv_rows、4 个 PD 控制模块 |
| Python AST | ok 79 个文件 | 固定 diff 中现存新增或修改 Python 文件 |
| Ruff | ok | standalone 46 文件使用 F/UP037；仓库文件按 hook 使用 F401/F821/UP037 |
| Ruff format | ok 79 个文件 | 检查模式，未改写 |
| standalone isort | ok | 使用 standalone 配置 |
| 仓库 isort | warn 1 个文件 | `disaggregation/ascend/conn.py` 的 import 分组缺空行 |
| shell 语法 | ok 4 个脚本 | `run_bm_startup_gate`、`run_gate`、`run_service`、`run_writer_gate` |
| 全量 diff 空白检查 | warn 128 处 | 已提交的 `numa-fabric-address-ranges.html` 行尾空白 |

**工具问题 T-01：** `conn.py:7–8` 缺少第三方 import 与项目 import 之间的空行。
**工具问题 T-02：** 历史 NUMA HTML 有 128 处行尾空白。
二者不计入 Standards/Spec 人工发现数量，也未在本轮自动修复。

本次检查范围比仅检查上一笔提交更大。
早先增量记录中的 isort/diff 通过，不能覆盖当前全范围检查结果。

复现 CPU 检查：

```bash
PYTHONPATH=ascend-mempool-test/src \
  /private/tmp/ascend-mempool-s1/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -q
```

复现类型检查：

```bash
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

本地原始日志位于 `/private/tmp/ticket03-s64-unit.log`、`ticket03-s64-mypy.log` 和 `ticket03-s64-static.json`。
未执行完整 SGLang registered suite，也未执行 NPU 测试。
轻量 suite 使用 CPU tensor 和 SDK 边界替身，不能证明真实远端读或 Graph replay 正确。

## 用户回传的 NPU 结果

证据来自本会话粘贴的终端输出。agent 没有直接读取远端报告文件。
结果在 `e838860ed0` 推送后的复验流程中回传，但输出未附 P/D 实际部署 SHA。
正式归档时需要补齐两端 SHA。

### 正式服务与小题目

| 检查 | 用户回传结果 | 证据范围 |
| --- | --- | --- |
| zero / decode / reuse | ok 1 / 32 / 32 tokens | 三个 HTTP 请求通过 |
| `verify_service.py` | ok `FORMAL_SERVICE_PASSED` | `--requests 3 --layers 78`；P/D 日志检查 |
| 铅笔小题目 | ok 25−7=18，18÷3=6 | 最终回答完整；153 completion tokens；`finish_reason=stop` |

checker 报告路径：`/tmp/ticket03-s6-checker/service-result.json`。
checker 通过代表其资源、Graph、生命周期与释放断言通过。
它不承担模型内容精度或性能判定，也不替代独立 fetch 数值测试。

### 三轮固定负载性能

负载为随机输入 128 tokens、输出 64 tokens、并发 1、每轮 64 个请求。
每轮预热 3 个请求，temperature=0，seed=42，启用 tokenize-prompt。
三轮共 192 个测量请求全部成功，另有 9 个预热请求。
每轮输入 8,192 tokens，生成 4,096 tokens。

| 指标 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 三轮中位数 |
| --- | ---: | ---: | ---: | ---: |
| 平均 TTFT（ms） | 931.762 | 899.922 | 910.199 | **910.199** |
| 平均 TPOT（ms） | 129.783 | 129.696 | 129.747 | **129.747** |
| 输出吞吐（tokens/s） | 7.026 | 7.055 | 7.044 | **7.044** |
| 平均 E2E（ms） | 9,108.08 | 9,070.75 | 9,084.23 | **9,084.23** |
| 测量时长（s） | 582.98 | 580.58 | 581.45 | **581.45** |

三轮平均 TPOT 极差约 0.087 ms，输出吞吐极差约 0.029 tokens/s。
这支持当前短请求负载运行稳定。
未提供同负载旧版本 A/B 数据，因此不能据此量化同步优化收益或严格证明无退化。

原始远端目录：`/tmp/ticket03-s6-perf.oLihG2`。
包含 `bench-1/2/3.jsonl` 和对应 `.log`。
本轮性能已测量；最终是否满足验收目标仍以用户确认为准。

```callout info 容量边界保持原约定
当前结论限于已测小容量和短请求负载。
长上下文、NUMA 窗口和大容量由用户在 Ticket09 独立处理。
不将其加入 Ticket03 的新阻塞项，也不外推本轮结果。
```

## 后续处理与验收边界

| 顺序 | 工作 | 完成判据 |
| --- | --- | --- |
| 1 | 处理 STD-F01 | 新容器符合规范；身份、hash、序列化与隔离语义不变 |
| 2 | 处理 SPEC-F01 | 删除不可达 shadow 组合；迁移有效覆盖；保留普通 staging 与正式 native abort |
| 3 | 清理 T-01 / T-02 | isort 与全量 diff 空白检查通过 |
| 4 | 复查整改并执行本地检查 | 关闭上述发现；记录最终目标 SHA |
| 5 | 执行最终版本 NPU 待项 | 双机 fetch、普通 sparse PD 实际路径、完整环境 registered 检查 |
| 6 | 补齐最终服务验收证据 | 按修改影响复验 checker、curl、性能；保存 P/D SHA 和用户确认 |

writer、资源或传输若在整改中改变，再运行对应组件 gate。
最终 fetch 使用已有 `verify_fetch.py`，覆盖 eager/replay、连续五步、padding 和独立数值基准。
普通模式必须实际运行 main-KV 传输、staging/host 写入和 host miss 读取。
CPU 模式矩阵不能替代该 NPU 回归。

既有请求仍在支持范围内的正确性问题，应在 Ticket03 修复。
不提前扩大到后续票的完整压力、取消故障矩阵或 AIME26 验收。
维护提示 P3 不要求无依据重构，也不增加新的性能门槛。

需求依据：[spec](spec.md)、[design](design.md)、[Ticket03](issues/03-prefill-direct-offload.md)、[S6 计划](ticket-03-s6-plan.md)、[阶段验收](verification.md)。
历史对照：[首次全量 review](ticket-03-s6-review.md)、[S6.2](ticket-03-s6.2-summary.md)、[S6.3](ticket-03-s6.3-summary.md)、[两轮同步交付](ticket-03-prepare-summary.md)。
运行入口：[正式服务](../../ascend-mempool-test/FORMAL_SERVICE.md)、[独立测试](../../ascend-mempool-test/README.md)。

**审查计数：Standards 1 项，最高 P2；Spec 1 项，最高 P2。另有 1 项非阻塞维护提示和 2 项工具问题。**
