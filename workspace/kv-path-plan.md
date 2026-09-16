# KV 路径实验

## 当前补充：原生 HiCache L2→L1（2026-09-16）

新增 `workspace/hicache_l2_bench/`，独立单机/单 NPU，无 Mooncake/L3。复用官方 NPU MLA pool、pinned Host pool、HiRadixCache 和 controller；仅测整请求纯搬运 `copy_whole` 和实际 L2 命中的 `hicache_load`（匹配/加载/提交/等待/完成维护），新测试不再设置固定页数拆批。默认一条命令先冒烟，再自动跑全部五档并保存结果。按阶段记录同一次 wall-clock 样本，不用中位数相减估算管理开销。单个连续 Host-only 前缀、无淘汰压力，不代表真实 server 调度或 L3 管理性能。先 128-token 冒烟，再扩大规模；脚本当前待远端 NPU 验证，运行命令与计时边界见新目录 README。历史 staging 数据保留，后续研究分析不再纳入 staging。流水不作为本轮前置任务。

## 当前传输实验要求

两套旧性能脚本均改为完整请求提交：不设置固定页数拆批；每个样本从完整请求开始到完成直接计时，预热也以完整请求为单位，不再累加独立批次样本。底层库内部的分片不由脚本干预。

- Mooncake 套件：本地 L2→L1、L3→L2→L1、L3→L2；不再测 staging。
- MemFabric 套件：L2→L1、L3→L2→L1、L3→L1、L3→L2。
- 两段路径先完成整个请求的 L3→L2，再提交整个请求的 L2→L1；不在本轮新增流水。
- 固定数据为 61 层 BF16 MLA、128-token page、512+64 维，一页一个 Store key。保持连续/确定性分散 L1 映射。
- 完整 L2 与 L1 容量随请求增长；128K 时各约 8.59 GiB。Mooncake 客户端池需相应扩容，MF 使用现有 10 GiB Host 池。
- 保留一页冒烟，再逐级扩展；默认完整请求预热 2 次、采样 10 次。清零、准备、校验在计时外，每轮校验。
- 若接口不支持完整列表或容量，记录具体错误并停止，不自动回退到小批次。
- 输出标记 `whole_request_v2`，保留原始样本、容量和统计。新的 HiCache L2 实验由另一 Agent 维护，本轮不修改。

运行与清理见 [Mooncake 套件](kv_path_bench/README.md) 和 [MF 套件](fabric_direct_bench/README.md)。本地检查不能代替远端 NPU 验证。

## 性能结果状态

2026-09-15 的 kv_path_bench/260915_110547 与 fabric_direct_bench/260915_104705 共 80 条正式性能结果整体撤回，等待新口径重测。原图和数据保留追溯，不继续引用其倍数、带宽或架构性能结论。功能验证与性能结论分开记录。
