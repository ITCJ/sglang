# 03: 正式 mempool 数据路径 cutover

**What to build:** 在02已验收的真实 shadow 双写/控制/readback 基础上，让 D attention
实际消费 mempool sparse fetch 的 KV；选择性移除原 main compact-KV transfer、staging
和长期 host KV allocation。保留 P native HBM cache、HBM Index K 及必要辅助传输。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [02: 16 对 rank 的正常控制闭环](02-rank-pair-control-lifecycle.md).

**Status:** ready-for-agent

**State:** open

## Acceptance criteria

- [ ] 复用02 backend runtime、P/D writer、统一 tick、准入和 drain，不重复开发真实 P
  offload；P native HBM cache 继续服务 chunked prefill。
- [ ] 将已验证的 P/D sparse fetch 接入 attention 输入；依据 prompt length/实际写入
  范围区分两个来源，保持 HBM sparse cache hit/miss 和重置行为正确。
- [ ] 两个来源的 copy 在 Graph 中均存在，含 zero-valid 路径；attention 等待 copy
  和相关写入完成，不产生冲突 destination。
- [ ] 逐项确认 buffer 后关闭 main compact-KV transfer/staging；Index K/state/aux/meta
  管理和传输保留，D 联合 readiness gate 仍成立。
- [ ] 移除重复长期 host KV；如果关闭旧 SparseKVCacheManager，先迁移仍需要的 HBM
  sparse cache/materialization 能力，mempool runtime 不依赖旧类生命周期。
- [ ] 保留 opt-in 和原有非 mempool 模式；cutover 的数据内容、Graph 与短请求 smoke
  由用户在 NPU 验证，正式模型对照矩阵由04完成。
- [ ] 按[阶段交付流程](../verification.md)核对实现、交付脚本并记录用户硬件验收。

## Verification

Mac 验证来源路由、边界及模式选择；用户运行 mempool attention 的 Graph/短请求
smoke，检查实际 fetch 内容、main KV traffic/staging/旧 host KV 确已移除，同时
Index K 和必要辅助传输继续运行。不能仅凭生成输出正常推断已切换路径。

## Comments


任务已建立；尚未实现，NPU 验收尚未执行。

2026-09-30：按用户确认更新票面，原真实 P offload 已归02 shadow；本票改为正式
数据路径 cutover，04负责正式集成验收。文件名保留以兼容现有链接。未实现/未验收。
