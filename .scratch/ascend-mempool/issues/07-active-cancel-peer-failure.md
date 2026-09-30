# 07: Active decode 取消与故障处理

**What to build:** 已进入 Graph decode 的请求在取消、peer/rank 故障或需要 retraction 时
停止新增工作，并根据可证明的 drain 安全释放；无法确认远端读取结束的 P slot 保持不可用，
故障明确报告。02提供基础报错/停止接线，本票扩展系统性 active cancel 与故障场景验证。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [04: 正式 mempool Graph 与模型集成验收](04-single-request-graph-decode.md).

**Status:** ready-for-agent

**State:** open

## Acceptance criteria

- [ ] active client disconnect、abort 或内部失败使请求停止未来调度；D drain 包含所有
  已提交 Graph、copy、offload 和 overlap extra forwards，再执行 completion exchange。
- [ ] 正常 active cancel 下，D drain 后释放 D slot 并发 `DONE`；P 自身 writes 完成且
  收到匹配 `DONE` 后释放 P slot，发送 `RELEASE_ACK`。`CANCEL` 本身不能证明 drain。
- [ ] ordinary receiver cleanup 后仍跟踪 active decode 的 peer dependence；
  peer/rank 故障可被观察并报告，停止提交依赖失效 pool 的新访问。
- [ ] 连接丢失、HTTP 结束、超时或 allocator free 不能触发未确认 P slot 的复用；
  无法证明 drain 时保留不可用 ownership，不做自动 reconnect、rebootstrap 或强制回收。
- [ ] 若请求需要 demo 不支持的 retraction，则明确终止并进入 cancellation，
  不静默丢弃远端 binding，也不把 request-pool release 当作 remote completion。
- [ ] 重复、迟到或错误 session/attempt/generation 的取消与 completion 消息不能释放新 owner。
  致命 collective 或硬件故障允许终止服务，但须清晰报告且不能继续不安全复用。
- [ ] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

交付用户可控的 active abort、overlap outstanding work、peer/rank failure 及 retraction
场景与运行命令。区分可正常 drain 的取消和无法确认 drain 的故障，检查 slot 是否按条件
释放或保持 unavailable。致命故障场景的验收为明确失败和无不安全复用，无自动恢复要求。

## Comments

任务已建立；尚未实现，NPU 验收尚未执行。

2026-09-30：同步D5：不可恢复故障直接报错终止、不做同进程恢复。02必须先具备
基础错误处理，本票负责完整故障矩阵；超时不能授权 slot 复用或未经确认的 BM 销毁。
