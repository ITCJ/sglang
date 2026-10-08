# 04: 正式 mempool Graph 与模型集成验收

**What to build:** 在03完成正式数据路径切换后，通过现有 PD HTTP 接口验证真实
GLM-5.1 请求、decode Graph replay、短 greedy baseline 对照和正常释放。
02已完成 shadow 服务；本票验证 attention 实际使用 BM 数据的正式路径，
不重复实现02 writer/control 或03 transfer/storage cutover。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [03: 正式 mempool 数据路径 cutover](03-prefill-direct-offload.md)（已关闭）、
[10: GLM-5.2 算法适配与 indexer 跨层复用](10-glm52-indexer-sharing.md)。

**Status:** ready-for-agent

**State:** open

## Acceptance criteria

- [ ] 单个真实请求完成 acquisition、binding、prefill、handoff、decode、drain 与 release；
  decode 入图并实际 replay，graph batch width 至少为 16，padding 不消耗真实 KV slot。
- [ ] 在 mempool 模式选择性关闭 main compact-KV 的原有 transfer 和 decode staging；
  Index K/state/aux/metadata 的原有管理与传输保持有效。BM 替代长期 host KV allocation。
- [ ] D 等待 matching `KV_READY` 与原有 transfer Success 在相关 ranks 上均满足；
  sparse HBM cache 的 miss 根据 prompt length 分成 P prompt 和 D decode 两个来源。
- [ ] 两条 UniDexCopy source path 均进入 Graph，即使 capture 时某来源 zero-valid；
  destination 不冲突，attention 等待两侧 copy 与相关 write 完成。
- [ ] D 将 forward 新产生的 compact KV 写入本地 BM；逻辑 token position 与实际 written
  length 一致，P 的首个 sampled output token 在 D subsequent forward 后才计入 decode KV。
- [ ] Graph 使用稳定地址与固定 device metadata buffers；请求行、binding、长度、index
  和 valid mask 更新正确，padded rows 不访问真实存储。
- [ ] sender/receiver handoff cleanup 后 persistent binding 仍可用；正常完成包含所有已提交
  Graph、copy、offload 和 overlap extra forwards 的 drain，然后 `DONE` / `RELEASE_ACK`。
- [ ] 使用固定短 greedy 请求与普通 sparse PD TransferEngine baseline 对比生成 token；
  对涉及的共享 hook 验证原有模式仍可运行，异常路径的完整验收留给 06/07。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

交付用户完整 P/D 服务、router 和 HTTP 请求的运行命令，以及 baseline 对照命令。
日志应证明真实 decode capture/replay、两侧 miss fetch、实际 decode offload 和最终安全释放。
核对短请求的 token 输出，报告实际 Mac 检查与 NPU 结果；eager-only 运行不满足本阶段验收。

## Comments

任务已建立；03完成后复用其正式路径，本票独立集成验收尚未执行。

2026-09-30：按用户确认校正阶段边界：02 shadow、03 cutover、04正式集成验收。
保留本票数据路径检查项作为03变更的系统验收，不要求重复实现。尚未实现/验收。

### 2026-10-07：03关闭；建议纳入P/D miss并行读取

用户确认03 S6 NPU验收完成，03依赖解除。用户提出两个来源写入的selected KV
目的位置互斥，要求解决当前P/D miss串行读取，并询问归属。本轮建议将其纳入本票，
作为Graph/fetch集成增强；具体范围待用户确认，本次不修改生产实现或宣称04已通过。

建议方案：

- 保留HBM hit独立stream；P prompt miss与D decode miss分别在独立stream提交。
- 公共metadata和两路indices准备完成后再分流；禁止在一个stream写metadata时另一个
  stream读取未就绪内容。共享scratch和跨layer/forward复用须等待所有消费者完成。
- D miss等待当前layer的D BM写入；保留P matching KV_READY/native readiness、binding、
  written-range、padding和互斥destination mask约束。
- 分别记录P/D完成事件，再合并为既有miss完成语义，或由消费者显式等待两路；
  attention、refill、forward完成和drain都必须覆盖新增stream。两路zero-valid仍入图。
- 扩展现有fetch gate，覆盖P-only、D-only、mixed、all-hit、zero-valid、连续异步
  forward和Graph replay；证明固定metadata复用不会被下一轮覆盖。
- NPU timeline验证实际重叠，比较修改前后同负载的mixed miss耗时及服务TTFT/TPOT。
  多stream只是允许并行，实际收益受算子资源和链路带宽限制，不能预先承诺加速。
- 完成原有短greedy token级普通sparse PD baseline对照和正常安全释放验收。
  不把03的人工小题目确认替代本票的固定输入token对照。

本建议不包含P端HBM/BM双写并行、NUMA窗口修复或后续票完整并发/故障矩阵。

### 2026-10-07：先执行GLM-5.2算法适配

用户要求继续本票之前先适配GLM-5.2，并明确创建10。增加10作为前置依赖，
执行顺序为03 → 10 → 04；本票P/D miss并行读取建议继续保留，未开始实现。
10负责模型算法适配及其回归，本票复用验收证据，再对后续Graph/fetch调度变更复验。
