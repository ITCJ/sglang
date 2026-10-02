# 03: 正式 mempool 数据路径 cutover

**What to build:** 在02已验收的真实 shadow 双写/控制/readback 基础上，让 D attention
实际消费 mempool sparse fetch 的 KV；选择性移除原 main compact-KV transfer、staging
和长期 host KV allocation。保留 P native HBM cache、HBM Index K 及必要辅助传输。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [02: 16 对 rank 的正常控制闭环](02-rank-pair-control-lifecycle.md).

**Status:** ready-for-agent

**State:** open

## 执行顺序（2026-10-02）

用户决定优先完成demo，NUMA/大容量分配排查已移到
[09](09-numa-allocation-followup.md)，延期且不阻塞本票；02剩余工作见其最新执行入口。
本票以02真实KV读回验证过的读取路径为基础，按以下顺序完成切换：

1. 将P/D BM sparse fetch接入D attention的selected KV输入，复用02的路由、索引、
   有效掩码及同步逻辑，继续验证HBM cache hit/miss和两路Graph copy。
2. 在完整的mempool模式切换中停用旧compact-KV offload、PD main-KV注册/传输与
   D staging/长期hostSHM申请。保留P原生HBM prefill cache、HBM Index K、HBM sparse
   cache/materialization及必要state/aux/metadata传输；逐个buffer核对其用途后调整。
   旧SparseKVCacheManager的这些剩余职责必须有明确承接位置。
3. 用小容量真实请求验收读取内容、Graph及释放，并证明旧hostSHM和main-KV traffic
   确已关闭；随后04做固定greedy请求的baseline对照。大容量性能复测在正式路径上按需
   恢复09，不把shadow阶段的双份DRAM占用当作最终服务的容量要求。

具体改动入口为NPU `sparsity_driven_kv_offload/attention.py`的selected KV materialization、
`manager.py`中的cache/host/staging职责，以及Ascend PD的main-KV buffer管理。
实现时复用02的mempool runtime/control，不再新建一套binding或ownership状态。

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
