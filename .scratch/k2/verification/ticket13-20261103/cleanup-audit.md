# ticket13 残留清理核查

基点 `dc31b37cbe` 已删除的人工 delay、临时 P/D 验收探针、旧搬运计时/poll，
沿用 `.scratch/k1/cleanup-test-hooks.md`，本票不重复登记删除功劳。

本票净迁移：

- `prefix.py` 的大 payload 文件写入移至 `mempool/observation.py:write_evidence`。
- 原调用保留短路径 marker，使既有功能验收器继续读取正确性文件。
- observe=false、verify_prefix=true 仍执行内容验证；没有把正确性验证捆绑到性能开关。
- 避免迁移时对同一 payload 额外做一次 JSON 序列化。

保留项逐类解释：

- manager/layout/sharing_plan 的64字节 probe 是正常启动映射身份校验。
- sparse manager 的 sentinel 是合法 padding/mask 索引，不是测性能的人工哨兵。
- source snapshot、native HBM内容比较、写入/range/source计数是票09正确性验收与协议依赖。
- 容量拒绝和故障注入仍在已有独立验收场景，未加入正常数据路径人工延迟。

检索范围：`disaggregation/ascend`、`hardware_backend/npu/mempool`、
`hardware_backend/npu/sparsity_driven_kv_offload` 的 Python 文件。
表达式：`probe|delay|sentinel|perf_trace|prefix_content`。
输出见 cleanup-rg.txt；命中逐项属上述正常逻辑、兼容marker或注释，没有残留人工delay。
