# 06: Acquire 等待及 decode 前取消

**What to build:** 合法请求遇到 slot 压力时在 waiting acquire queue 等待，
统一 preflight 后意外部分 acquire 失败报错终止；bootstrap timeout、disconnect 或 abort
能取消 decode 前的请求并安全释放资源，迟到消息不会重新 acquire 已取消的请求。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [04: 正式 mempool Graph 与模型集成验收](04-single-request-graph-decode.md).

**Status:** ready-for-agent

**State:** open

## Acceptance criteria

- [ ] 分别覆盖 D 和 P 的 slot 不足：请求等待临时容量，容量恢复后可继续；
  统一 preflight 后意外部分 acquire 失败进入 fail-stop；普通取消仍支持安全 rollback。
- [ ] acquire waiting 纳入原有 PD bootstrap deadline，跨队列和 retry 不重置 deadline；
  router/client disconnect、显式 abort 和 transfer failure 可触发更早取消。
- [ ] 在 waiting、D acquired、P acquired、binding、prefill 和原有 transfer 期间取消，
  两侧状态按实际 ownership 收敛；已有 writes 排空前不释放被访问的 slot。
- [ ] `KV_READY` 或 Index K/state/metadata 任一条件缺失或失败均不进入 decode，
  无论 readiness 消息的到达顺序如何。
- [ ] request attempt 和 slot generation 校验配合 terminal identity，拒绝 cancel 后迟到的
  `ACQUIRE`、`ACQUIRED`、`BOUND_ACK` 和 `KV_READY`；重复 cancel/rollback 幂等。
- [ ] 明确区分无 outstanding access 的安全 rollback 与已有访问时的 drain/release；
  原有 `ABORT_ACK` 不被视为 D decode drain。正常取消后受影响 slot 可安全服务后续请求。
- [ ] prompt/decode/context/HBM 容量超限的请求明确失败，暂时 slot 压力进入等待；
  测试可通过已有消息/操作边界制造压力和 partial acquire 失败，不要求 05 先完成。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

Mac 上通过消息/操作边界验证 rollback、deadline、重复消息和 late acquire。
交付用户等待超时、client disconnect、partial-rank acquire 失败、prefill/transfer 取消的
受控 NPU 测试；检查请求结果、16 ranks 状态、drain 和 slot 可复用性。
使用可控占用或故障注入构造容量压力，避免依赖尚未完成的 batch ticket。

## Comments

任务已建立；尚未实现，NPU 验收尚未执行。

2026-09-29：同步用户确认的 D1，意外部分 acquire 失败采用 fail-stop，替代原先
rollback 后继续等待；容量不足仍等待，普通取消的安全 rollback 保留。仅更新文档。
