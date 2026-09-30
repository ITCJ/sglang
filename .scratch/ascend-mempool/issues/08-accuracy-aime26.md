# 08: 精度对比与 AIME26 验收

**What to build:** 用相同权重、请求和推理配置，对比 mempool Graph 路径与现有 sparse PD
TransferEngine baseline 的固定 greedy 输出和 AIME26 成绩，调查差异并记录精度结论。
正确性通过后记录性能，供后续优化使用。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [05: 连续请求、batch 与 slot 复用](05-batch-slot-reuse.md).

**Status:** ready-for-agent

**State:** open

## Acceptance criteria

- [ ] baseline 与 mempool 使用相同本地 GLM-5.1 权重、测试输入、推理参数和相应运行环境，
  固定短请求及较长 greedy 请求的 token sequences 一致；任何差异均被定位并解决。
- [ ] 在同一 AIME26 测试集上使用 temperature 0、max output tokens 28672 的既有评估流程，
  mempool 得分不低于普通 sparse PD TransferEngine baseline；逐题保留输出以调查差异。
- [ ] 提升可配置 `S_D` 以覆盖评估上限，并核对 prompt+decode context、Index K HBM、
  P native KV、对齐后 BM 容量和 UniDexCopy 范围；不可服务配置清晰失败。
- [ ] 评估实际使用 decode Graph replay，并覆盖长 decode、多次请求与 slot 复用，
  记录超时、失败、截断和取消的请求，避免把未完成评估解释为通过。
- [ ] 正确性通过后记录延迟、吞吐和相关容量配置；本 ticket 不设固定性能提升门槛。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录 baseline/mempool 命令、代码版本、逐题输出、成绩与确认结论。

## Verification

交付用户可对照执行的 baseline 与 mempool 评估命令、同一输入集、token 比对脚本、
AIME26 打分和差异报告流程，说明长上下文所需配置与日志。Mac 上检查可执行的比对和
汇总逻辑；完整生成与精度验收由用户在 NPU 执行。
本 ticket 通过只代表精度阶段完成；整个 feature 仍要求 06/07 等全部 ticket 验收通过。

## Comments

任务已建立；尚未实现，NPU 验收尚未执行。
