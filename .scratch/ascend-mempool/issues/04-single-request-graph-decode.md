# 04: 正式 mempool Graph 与模型集成验收

**What to build:** 在03完成正式数据路径切换后，通过现有 PD HTTP 接口验证真实
GLM-5.1 请求、decode Graph replay、短 greedy baseline 对照和正常释放。
02已完成 shadow 服务；本票验证 attention 实际使用 BM 数据的正式路径，
不重复实现02 writer/control 或03 transfer/storage cutover。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [03: 正式 mempool 数据路径 cutover](03-prefill-direct-offload.md).

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

任务已建立；尚未实现，NPU 验收尚未执行。

2026-09-30：按用户确认校正阶段边界：02 shadow、03 cutover、04正式集成验收。
保留本票数据路径检查项作为03变更的系统验收，不要求重复实现。尚未实现/验收。
