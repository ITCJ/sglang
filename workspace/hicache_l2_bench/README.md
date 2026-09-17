# 单实例原生 HiCache L2→L1 测试

> 最新结果：用户于 2026-09-16 提供的截图已归档到 [results/260916_162313_897656](results/260916_162313_897656/EXTRACTION.md)，跨实验结论见 [最新分析](../bench_analysis/260916/analysis.md)。本次三组为当前基准，旧性能结果无效。截图未包含远端 commit、实际命令或校验开关；正文中“尚需远端验证”是交付时状态，不能用汇总截图替代数据正确性证明。

目的：先判断官方 pinned Host L2 上的搬运性能，同时记录真实 HiCache 加载的管理阶段。无 Mooncake、MF、L3、模型权重或推理。使用当前 checkout 的 SGLang 源码，单进程/单 NPU/TP=1，临时文件初始化单 rank Gloo，不占用 rendezvous TCP 端口。

## 测什么

实际构造 `NPUMLATokenToKVPool`、`HiRadixCache`（内部构造 `MLATokenToKVPoolHost` 和 `HiCacheController`）及 `PagedTokenToKVPoolAllocator`。Host 使用官方 `torch.empty(..., pin_memory=True)` 分配，检测实际 pinned 状态，不回退到 ADXL 或普通 CPU 内存。`model_path=dummy` 仅跳过模型参数解析，未替换缓存实现。

| 输出路径 | 计时范围 |
| --- | --- |
| `copy_whole` | 地址预备后，将整个请求交给相同加载接口，一次同步 |
| `hicache_load` | `match_prefix` → `init_load_back` → `ready_to_load_host_cache` → 等真实完成事件 → `loading_check` |

`copy_whole` 经 `load_to_device_per_layer(..., layer_id=0, io_backend=kernel_ascend)` 调用 SGLKernel；Ascend MLA 在第 0 层调用时搬运全部层。`hicache_load` 由真实 controller 遍历层接口及记录事件，不改变生产代码。

默认模拟 DeepSeek V3.1 MLA：61 层、BF16、128 tokens/page、压缩 KV 512 + RoPE 64。默认测试连续和真正离散两种 L2/L1 页映射。同一映射下，纯搬运和真实 HiCache 加载使用相同的 Host/NPU 池配置、有效页映射和数据；第 0 个 NPU page 保留。

离散模式将物理池扩大为两倍：Host L2 使用 page slots `0,2,4,...`，NPU L1 使用 `1,3,5,...`，因此每两个有效 page 之间都有一个完整 page 大小的空洞，而每个 page 内的 128 tokens 仍连续。每轮在计时外设置真实 Host/NPU allocator 的 free list，两条路径仍由真实 `alloc()` 分配。逐字节校验同时检查有效数据、NPU 保留页以及 L2/L1 空洞未被覆盖。JSON 记录两层实际 slots、物理容量和 `mapping_version=page_gap_v2`。这是确定性空洞布局，不模拟长期运行的随机碎片。

128-token 单页只能作为启动冒烟，不能验证 page 之间的空洞；1K 的 L1 slots 为 `[1,3,5,7,9,11,13,15]`。三套实验统一使用该定义。旧结果中的 `scattered` 仅为连续物理页集合上的顺序置换，不作为本轮真正离散结果。

每组每轮前清零目标 NPU，重置分配器及树，构造一个完整 Host-only 前缀。Host KV 根据 page/layer/token/component 生成；性能模式默认关闭数据校验（含 128-token 冒烟）；加 `--validate` 才会每轮计时后逐页逐字节校验并检查保留页。JSON 记录 `validation_enabled`，未校验时 `correct=null`，全部校验通过才为 `true`。构造、清零、校验和原始样本写盘均不计时。预热和采样按整个请求执行，两组轮换顺序，默认预热 2 次、采样 10 次。128-token 冒烟各执行一次，不用于稳定性判断。

本实验不在脚本层拆分请求：`copy_whole` 一次提交该任务的全部页，`hicache_load` 由真实 controller 提交该请求的加载任务。底层 kernel 自身仍可按页执行，这是其实现而非实验设置。两组都对完整请求计时，不累加独立批次的采样。正式加载组一次执行同时保存总耗时与内部阶段耗时；`copy_whole` 仅作为独立的纯搬运参考。

## 管理阶段口径

JSON 保留每个样本的以下秒数；CSV 的 median/p95 单位 ms：

- `match_s`：真实前缀匹配。
- `load_back_s`：节点遍历、保护/引用状态、NPU 页分配、加载入队。
- `submit_s`：controller 合并任务、索引转换、加载提交、逐层事件记录。
- `wait_s`：提交返回后到完成事件同步返回的剩余等待。
- `acknowledge_s`：真实完成检查、ack 出队及引用计数维护。
- `total_s`：上述五段的同一次 wall-clock 总时间。
- `submit_to_complete_s`：submit + wait。
- `controller_stream_s`：若后端支持 timing events，记录 controller 的设备流区间；包含事件等开销，不称为纯 DMA 时间。

`submit_s` 可能与设备传输重叠，`load_back_s` 可包含 NPU 索引操作，因此不能把这些都解释为纯 CPU 时间。`controller_stream_s` 与 wall-clock 阶段也不能相加。不用“加载组中位数减搬运组中位数”估算管理开销。

这是单个完整命中的合成前缀，没有分支丰富的 radix 树、内存压力、淘汰、请求竞争、调度器轮询间隔或跨 rank 同步成本。Host-only 节点用真实 `_insert_helper_host` 在计时外建立；测的是 L2→L1 加载管理，不是 L3 元数据管理，更不是 server TTFT。直接在匹配后提交任务，排队等待由实验固定为零。

## 远端执行

前置：单机 A3，现有 CANN / torch_npu / sgl_kernel_npu 与 SGLang 运行依赖完整；设备空闲，自己的模型和其他性能实验已停止。无需源端、Store 或第二台机器。代码需先经用户明确要求后提交/push；远端再执行：

```sh
git pull --ff-only
git log -1 --oneline
python3 workspace/hicache_l2_bench/run.py
```

**默认命令一次完成全部测试**：两种布局的 128-token 冒烟通过后自动测全部五档 × 两种布局，每个组合输出两条路径并实时保存，共 20 条正式路径结果。无需手工切换 tokens。`--suite` 是同样行为的显式写法。成功标志为 `L2_ALL_OK`，默认只表示运行完成，启用 `--validate` 后才包含数据校验通过。

如需在性能套件开头增加一次小规模传输与数据校验，使用聚合入口：

```sh
python3 workspace/hicache_l2_bench/run.py --preflight-validate
```

它先运行一次 4K scatter、0 预热、1 次采样并逐字节校验；通过后自动继续完整的连续+离散性能矩阵，性能部分不校验。校验失败会立即停止，不会启动性能矩阵。不需要开头校验时直接运行默认命令。`--validate` 仍保留为整套逐轮校验的诊断选项，不能与 `--preflight-validate` 同时使用。关闭数据校验仍保留 pinned 状态、分配映射、命中、完成事件及 ack 状态检查。

只测离散（全部五档），或运行冒烟/单档时，可使用：

```sh
python3 workspace/hicache_l2_bench/run.py --scatter
python3 workspace/hicache_l2_bench/run.py --smoke
python3 workspace/hicache_l2_bench/run.py --tokens 1024
python3 workspace/hicache_l2_bench/run.py --tokens 16384 --scatter
```

已有连续布局结果保留；新增离散模式及 runner 均需在远端执行环境验证。

套件先冒烟，再测 1K/4K/16K/64K/128K，每个规模/布局组合独立进程。128K 连续布局的 Host/NPU KV 池各约 8.59 GiB，离散布局各约 17.16 GiB（均不含库额外资源）。默认每档超时 1800 秒，是防挂死上限。可传 `--device N --warmup 2 --repeats 10 --timeout 1800`。

失败停止后续规模。诊断只需一条命令：

```sh
python3 workspace/hicache_l2_bench/run.py --diagnose
```

回报 `L2_FAIL` 行及诊断输出的最后 35 行。结果保存于本目录 `results/<timestamp>/`：`cli.log`、`status.json`、各组合的 `case-<tokens>-<layout>.log/.json/.csv`、`summary.csv`。CSV 和终端包含 `layout`，两种布局不会覆盖；旧版连续结果文件保持原样。有效带宽为十进制 GB/s；默认 10 样本的 nearest-rank P95 是最大值。JSON 记录提交、导入源码位置、库版本、实际池容量和 pinned 状态；CANN/驱动/镜像版本应随实验回报，不能从 torch 版本推断。

退出或 Ctrl+C 时 runner 回收自己启动的子进程组；无常驻服务要手工停止。每档进程结束释放内存。失败日志保留，不自动安装或修改环境。
