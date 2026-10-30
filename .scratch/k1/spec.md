# K1 实施依据

**2026-10-29执行范围更新（用户指令优先）：** 性能埋点、perf配置及运行工具仅保留在
`cryang/dev/mempool-min-baselinetest`，当前工程分支只保存历史测试结果。
K1功能实现与NUMA验收继续推进；下文性能复测、基线并列和遗留指标重报条款
须在测试分支另行安排，不计作本分支已实现或已验证。

**Status:** ready-for-agent

**State:** open

本地规格采用 [K1 拆票定稿](k1-tickets.md)及下列十张票。
用户指定 [syncfile K1 目录](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/k1/)为依据。
执行文本和[基线目标](k1-01-baseline-prompt.md)按原文同步；各票仅补充本地状态、依赖链接和 Comments。
设计决策回查 [mempool-min 讨论](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/discussion.md)及[完整设计讨论](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-design/discussion.md)。

## 票据索引

| 序号 | 票据 | 直接依赖 |
|---|---|---|
| 01 | [K1-02a 显式映射启动与页驻留证据](issues/01-k1-02a-explicit-numa-mapping.md) | 无 |
| 02 | [K1-02b 共享池适配器接入与记账骨架](issues/02-k1-02b-shared-pool-adapter-share0.md) | 无 |
| 03 | [K1-04 prefix split_read](issues/03-k1-04-prefix-split-read.md) | 无 |
| 04 | [K1-05c dense 读带宽微测](issues/04-k1-05c-dense-read-bandwidth-microbench.md) | 无；不阻塞其他票 |
| 05 | [K1-03a P alive 主 KV 共享](issues/05-k1-03a-p-alive-main-kv-sharing.md) | 01、02 |
| 06 | [K1-03b D alive 主 KV 共享与每步顺序](issues/06-k1-03b-d-alive-main-kv-sharing-step-order.md) | 01、02 |
| 07 | [K1-05a P alive Index K 单份](issues/07-k1-05a-p-alive-index-k-single-copy.md) | 01、02 |
| 08 | [K1-05b prefix 单份与 NUMA interleave](issues/08-k1-05b-prefix-single-copy-numa-interleave.md) | 05、03、07 |
| 09 | [K1-06 完整验收](issues/09-k1-06-full-acceptance.md) | 06、08 |
| 10 | [X 旁路定位](issues/10-x-baseline-anomaly-triage.md) | 无；不阻塞其他票 |

## Comments

### 2026-10-29：替换旧草案

按用户要求删除本地四份 K1 草案文件，导入 syncfile 的十票定稿。
全部 K1 票保持 open，功能实现和对应 NPU 验收尚未开始。
单请求性能基线已在 `134b22d90d` 经用户验收；当前 worktree 尚未合入该版本。
本次只同步文档；实施前接入已验收的 perf 入口与基线记录。

本地核对通过：12份源文档内容一致（票据排除上述本地元数据），26个链接有效，
旧草案及引用已清除。进度页HTML与Markdown一致，STE零警告，`git diff --check`通过。

### 2026-10-29：K1-01实现交付

已验收基线单独导入为`7a8a0b955b`；01显式NUMA映射及证据代码已交付。
505项CPU、735子测试和两轴审查通过；NPU验收未执行，全部票保持open。
详见[01交付记录](delivery-ticket01.md)。其他九票未开始实施。

### 2026-10-29：移除perf后重新交付

按用户指令保留baselinetest分支，工程分支仅保留历史基线结果。
K1-01 NUMA功能保留，双映射功能验收不再运行perf。
移除后的完整476项CPU、735子测试、7模块strict mypy和两轴审查通过。
旧505项记录仅描述原版本；NPU未执行，所有票仍open。


### 2026-10-30：K1-02 用户验收通过

用户在31/32运行`k1-ticket02-004`后明确确认“没问题，可以验收”。
交付版本`fa97a49820`；02按两轮精简验收和SGLang默认退出约定关闭。
[验收记录](verification/k1-ticket02-004-20261030/acceptance.md)保留证据来源与范围。
01/02均已用户验收，05/06/07直接依赖满足；其余八票保持open，本次未继续实现后续票。

### 2026-10-30：K1-03 用户验收通过并授权清理

用户确认0337b9b9ad在31/32运行k1-ticket03-006的双策略功能验收通过。
03已closed；[确认与范围](verification/k1-ticket03-006-20261030/acceptance.md)独立归档。
临时工程测试代码已按约定清理，492项CPU、静态检查与双轴复审通过；清理后未重跑NPU。
08的03依赖满足，但仍等待05、07各自验收；本次不开始后续票。
