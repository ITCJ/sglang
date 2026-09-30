# 05: 连续请求、batch 与 slot 复用

**What to build:** 在同一个服务和已 capture 的 Graph 上连续运行不同长度的请求，
验证多请求 batch、不同 P/D slot、request-row 和 physical slot 复用均返回正确 KV 和输出，
重复或延迟 completion 消息不会影响新的 slot owner。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [04: 正式 mempool Graph 与模型集成验收](04-single-request-graph-decode.md).

**Status:** ready-for-agent

**State:** open

## Acceptance criteria

- [ ] 连续请求改变 prompt/decode 长度、P/D slot、request-pool row 与 sparse index，
  无需按请求重新 capture；输出与对应普通 sparse PD baseline 一致。
- [ ] 覆盖 `p_slot != d_slot` 和 prompt/decode 分界，slot 绑定按请求身份维护，
  不从 `req_pool_idx` 或 Graph padded row 隐式推断 physical slot。
- [ ] 在实际 HBM 与 16 physical slots 允许的范围内运行多请求 batch；真实请求与 padded
  rows 的 mask 正确，padded rows 不 acquire slot、不读写其他请求数据。
- [ ] request-row 或 physical slot 复用时清理 sparse cache 的旧映射和状态；
  sentinel KV 与真实生成检查均未发现旧请求 cache hit 或跨请求污染。
- [ ] 同一 physical slot 的新 generation 不受旧 attempt/session、重复或迟到的 `DONE`
  和 `RELEASE_ACK` 影响；先前控制记录只能完成自身 acknowledgement。
- [ ] 在真实服务复用与消息/操作边界验证中，只有 drain 已确认的 slot 可分配给新请求，
  slot 计数和所有权在重复复用后稳定。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

交付用户一个连续请求及 batch 测试脚本，明确长度序列、并发量、重复次数、baseline
和内容/输出判据。为不同 P/D slot 和 late completion 提供可复现的受控场景；
记录 slot generation、Graph replay 与 sparse cache reset 的可观察证据。

## Comments

任务已建立；尚未实现，NPU 验收尚未执行。
