# KV 路径实验

## 当前执行入口：仅三条 UNIDEX

用户允许暂跳过尚未通过的 SysV：统一入口加 `--skip-sysv`，只采 BM 本地/远端两条路径（20 条正式结果），显式记录 skipped_paths；不改变 BM 布局/计时，不将 SysV 标为通过。默认三路径入口保留。

统一使用 [unidex_copy_bench/run.py](unidex_copy_bench/README.md)：源端提供 BM 内存，客户端依次测 BM local→L1、BM remote Host→L1，释放 BM 后测 SysV L2→L1。只输出这三条新增路径，五档 × 两映射共 30 条正式结果，不重跑原四条 BM 对照。默认性能及 smoke 不校验；首次先以同一入口 `--check-only` 做小规模校验，`--preflight-validate` 可选地为各后端增加 128 contiguous/4K scattered 校验后运行性能。所有布局、计时和归因边界保持下述定义；该入口尚待远端验证。

## 当前补充：UNIDEX / SysV registered Host（2026-09-28）

**新增实现，待远端验证。** 原单机 UNIDEX + SysV registered Host L2→L1 保留；在现有 [MemFabric 双端基准](fabric_direct_bench/README.md#unidex-bm-映射补充实验) 增加显式开启的 BM local→L1 和 BM remote Host→L1 UNIDEX 路径。依据 [远端 benchmark `f934478`](https://github.com/hibikid/ascend-ub-bench/blob/f934478756ab5be92cfe409a3f6bc3baaf4b207f/remote_dram_sparse_copy_bench.py#L764-L785)，把 BM GVA 映射为 `LOCAL_DEVICE` 地址后交给原 `src_ptr`，不升级 kernel 或新增传输协议。两条新路径沿用同一已填充远端源、逻辑 KV、61 层 BF16、128-token page、分离 K/RoPE MLA 布局、slot 0、散页映射和五档规模；本地来源分别标记 SysV 与 BM。远端源按每页先全部 K 再全部 RoPE，本地 BM L2 为全局 K/RoPE 两个区域，各按真实字节布局构造索引，单视图小于 4 GiB。源静态准备和映射在计时外，索引准备单列，每个完整请求样本计入全部 launch 和完成同步，保留 warmup2/repeats10、median/p95。性能默认不做数据校验；先按双端 README 独立校验小规模。缺映射或容量即失败，不改页粒度、批次或标为 relay。目标机尚无该路径结果；同事单层 576 top-k 多次调用一次同步的平均值不可直接与整请求结果相比。

归因边界：SysV 是首个本地入口有意选用的 Host 来源；此前只准备 SysV L2、未纳入 BM 映射，是信息不足造成的范围缩窄。L3 每页 K/RoPE 与 L2 整池分区是原实验既有布局，本次补的是 BM 远端映射和匹配两种原布局的地址适配。

## 当前补充：原生 HiCache L2→L1（2026-09-16）

新增 `workspace/hicache_l2_bench/`，独立单机/单 NPU，无 Mooncake/L3。复用官方 NPU MLA pool、pinned Host pool、HiRadixCache 和 controller；仅测整请求纯搬运 `copy_whole` 和实际 L2 命中的 `hicache_load`（匹配/加载/提交/等待/完成维护），新测试不再设置固定页数拆批。默认一条命令先冒烟，再自动跑全部五档并保存结果。按阶段记录同一次 wall-clock 样本，不用中位数相减估算管理开销。单个连续 Host-only 前缀、无淘汰压力，不代表真实 server 调度或 L3 管理性能。先 128-token 冒烟，再扩大规模；已收到 2026-09-16 远端汇总截图，实际 commit 与校验状态未回传；运行命令与计时边界见新目录 README。历史 staging 数据保留，后续研究分析不再纳入 staging。流水不作为本轮前置任务。

## 当前传输实验要求

两套旧性能脚本均改为完整请求提交：不设置固定页数拆批；每个样本从完整请求开始到完成直接计时，预热也以完整请求为单位，不再累加独立批次样本。底层库内部的分片不由脚本干预。

- Mooncake 套件：本地 L2→L1、L3→L2→L1、L3→L2；不再测 staging。
- MemFabric 套件：L2→L1、L3→L2→L1、L3→L1、L3→L2。
- 两段路径先完成整个请求的 L3→L2，再提交整个请求的 L2→L1；不在本轮新增流水。
- 固定数据为 61 层 BF16 MLA、128-token page、512+64 维，一页一个 Store key。连续布局使用相邻物理 page；真正离散布局在可控的 L2/L1/远端 Host 中让有效 page 之间间隔一个完整空 page，page 内部仍连续。
- 完整 L2 与 L1 容量随请求增长；128K 连续布局约 8.59 GiB，离散布局约 17.16 GiB。Mooncake 客户端池按布局扩容；MF 性能模式两端使用 28 GiB Host 池以同时保存连续和离散源/目标地址范围。
- 保留一页冒烟，再逐级扩展；默认完整请求预热 2 次、采样 10 次。三个套件都提供可选 `--preflight-validate` 聚合入口：先做一次 4K scatter 单次逐字节校验，通过后自动运行无数据校验的完整性能矩阵。`--validate` 仅保留为整套诊断选项。
- 若接口不支持完整列表或容量，记录具体错误并停止，不自动回退到小批次。
- 输出标记 `whole_request_v2`，保留原始样本、容量和统计。HiCache L2 单独记录真实管理阶段，不与传输套件混同。

运行与清理见 [Mooncake 套件](kv_path_bench/README.md) 和 [MF 套件](fabric_direct_bench/README.md)。所有实验和测试只在用户的远端执行环境运行。

三套实验完成后，在保存结果的远端仓库运行：

```sh
python3 workspace/collect_latest_bench_results.py
```

脚本分别选择三个 `results/` 目录中名称最新的 run，严格读取该 run 的 `summary.csv`，并输出适合截图/OCR 的精简 `workspace/latest_bench_summary.csv`。CSV 只保留 `experiment,tokens,layout,path,metric,median_ms,p95_ms,effective_gbps`，过滤 smoke 行；run ID 和源文件路径只打印在终端。输出按 55 条数据分页，每页为 `# PAGE x/y`、重复表头和最多 55 条数据，合计不超过 57 行。若最新 run 没有 `summary.csv`，脚本报错，不会静默回退到旧结果。可用 `--output <PATH>` 改变输出位置。

## 性能结果状态

2026-09-15 的 kv_path_bench/260915_110547 与 fabric_direct_bench/260915_104705 共 80 条正式性能结果整体标记为无效，已由 2026-09-16 最新结果替代。原图和数据保留追溯，不继续引用其倍数、带宽或架构性能结论。功能验证与性能结论分开记录。


当前结果基准为 HiCache `260916_162313_897656`、Mooncake `260916_174640`、MemFabric `260916_174238`，原图与转录 CSV 均保存在各实验 results 目录。分析见 [2026-09-16 报告](bench_analysis/260916/analysis.md)。仅收到汇总截图，不能推断校验已开启或远端版本已核验。下一步优先分离 pinned/ADXL/BM Host 与加载实现的影响，并在 Mooncake 同一次完整路径执行中记录两段时间；本轮不新增运行任务。
