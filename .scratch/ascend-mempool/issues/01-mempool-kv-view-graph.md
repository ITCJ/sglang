# 01: 双机 mempool KV view 与 Graph 验证

**What to build:** 在一对真实 P/D NPU 上，通过可复用的 typed mempool KV view
管理 P prompt KV 和 D decode KV，让 D 用同一个 NPU Graph 从两侧选取 BF16 KV，
并逐元素验证 eager 和 replay 的结果。此阶段确立远端 BM 映射与 Graph 的硬件可行性。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

**State:** closed

## Acceptance criteria

- [x] 在同一 superpod 的两台机器上建立一个两-rank BM pool，P 为 pool rank 0，
  D 为 pool rank 1；D 在映射验证和固定缓冲区初始化完成后才 capture。
- [x] KV view 接受逻辑 layer/slot/token 坐标，管理 shape、BF16 dtype、stride、
  映射地址、边界和 pool lifetime；调用者能通过逻辑坐标读写，不需手算 rank 基址。
- [x] 按 layer slab 和 `[B_slots, S, N, D]` 计算容量，区分 P/D token capacity；
  两侧使用各自对齐后的实际贡献和一致的 common maximum stride。
  依据所选 BM backend 对齐，并在启动时拒绝超出 UniDexCopy span 或 row 限制的布局。
- [x] 使用不同 P/D 实际贡献大小实测地址和内容正确；区分 GVA 与当前进程的 device VA，
  供 kernel 使用的映射地址有效，pool 和映射在 Graph 使用期间保持存活。
- [x] P 写入已知 prompt KV，D 写入已知 decode KV；D 的两条 UniDexCopy 路径
  在 eager、capture 和多次 replay 中均逐元素匹配预期 BF16 数据。
- [x] 不重新 capture 即可改变 slot、长度、index 和 valid mask；覆盖边界位置、
  任一来源 zero-valid、两来源同时有效以及 padded rows，padded rows 不访问真实 slot。
- [x] 按[阶段交付流程](../verification.md)完成实现核对、测试脚本交付和用户 NPU 验收，
  在 `Comments` 中记录实际证据。

## Verification

Mac 上检查布局、边界、逻辑索引和配置校验等可独立执行的行为。
交付用户一套双机测试命令，覆盖 eager、capture/replay、动态绑定和非对称贡献；
报告每个场景的内容比对结果及配置。旧 eager benchmark 通过不能替代本阶段验收。

## Comments

建票时尚未实现，NPU 验收尚未执行。

### Implement 01: 实现与人工验收交付

- 独立实现提交：`5a87606304`，branch `cryang/dev/mempool`。人工验收时两机使用同一代码版本。
- 用户确认本阶段独立代码放在仓库根目录 `ascend-mempool-test/`，下设 `src/`、
  `scripts/`、`tests/unit/`；不启动 SGLang 服务，不加入既有测试目录。
- 目标 NPU 环境为 MemFabric Hybrid 1.1.4；API 参考本地 `release/1.1` 的 `9fa9afbb`。
  使用 `create2`、独立实际贡献、共同 maximum stride、GVA 到 local-device mapping。
- 实现 BF16 layer layout、typed manager/view、固定 device inputs 和两来源 UniDexCopy。
  使用 2 字节 CPU dtype 占位及显式 row extents，避免 Meta dispatch 抢先选中 kernel。
- 双机脚本按块写入已知 KV，先 eager，再 capture/replay 改变 binding/length/index/mask；
  10 种 case 包含 zero-valid、padding 和边界。capture 时 D source 无有效行，后续 replay 验证 D 路径。
- `run_gate.sh` 串行运行等容量和不等容量测试；D drain 后通知 P 释放，保存各侧日志和 JSON。
  运行命令与回传清单见[独立测试说明](../../../ascend-mempool-test/README.md)。
- Mac 实际执行完整 CPU suite：13 项测试通过，覆盖布局、view lifetime、消息 framing、
  CPU Tensor 索引和逐元素内容比对。mypy strict、ruff check/format、isort check、
  `bash -n` 均通过；标准库 `--describe` 验证了 1/1 GiB 与 1/2 GiB contribution。
- code-review 分别执行 Standards 与 Spec 检查，未发现其他确定性问题。自查修复了
  cleanup 失败仍可能留下成功标记的问题，已完成 focused recheck；成功标记只在
  pool、全局 BM/MF、channel cleanup 和报告写入均成功后输出。
- 交付时仍等待用户执行 NPU 验收；实际结果及关闭结论见下文。

### 2026-09-27: 双机 NPU 验收通过，关闭 01

- 用户在同一 superpod 的 `npu1-31` (P/rank 0) 与 `npu1-32` (D/rank 1)
  人工执行 `--check-env`：两侧 MemFabric Hybrid 1.1.4、torch_npu 2.10.0、
  sgl_kernel_npu 2026.9.0、Ascend910_9382，且 UniDexCopy schema 提供
  `src_rows`、`src_ptr`。双机使用上述独立实现；NPU checkout 的完整 HEAD
  未另行提供，代码基线以提交 `5a87606304` 记录。
- P 命令：`bash ascend-mempool-test/scripts/run_gate.sh 0 10.120.72.31 tcp://10.120.72.31:24670 0 /tmp/mempool-01-p`。
  D 命令：`bash ascend-mempool-test/scripts/run_gate.sh 1 10.120.72.31 tcp://10.120.72.32:24670 0 /tmp/mempool-01-d`。
  日志由用户在会话中提供；runner 的原始 `.log`/`.json` 文件留在两机上述路径，
  未直接读取这些文件。
- 对称轮：P/D 各贡献 1 GiB，common stride 1 GiB；非对称轮：P 1 GiB、
  D 2 GiB，common stride 2 GiB。两侧均完成 MAPPED、STAGED，并输出
  `ALL_CHECKS_PASSED`。D 每轮输出 62 条 PASS：24/48 core，各 10 种 eager、
  首次 capture replay、两轮 10 种动态 replay；每条比对 1,179,648 个 BF16 元素。
  zero-valid、padding、边界和 P/D 两来源用逐元素比较覆盖。
- 启动日志中的可选扩展库未设置、默认 tag 首次查询及 join 前旧 key 不存在
  (`-404`) 均不阻断本路径。peer GVA 映射的暂态失败经等待后恢复；后续
  Graph 远端读逐元素通过。`torch_npu` 的 base-format warning 未影响检查。
- 用户确认 01 已完成。依据脚本只有在 pool、BM/MF、channel cleanup 和报告写入
  成功后才输出 `ALL_CHECKS_PASSED`，本次日志满足验收；关闭 01，解除 02 blocker。
