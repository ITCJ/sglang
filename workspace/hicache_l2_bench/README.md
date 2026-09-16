# 单实例原生 HiCache L2→L1 测试

目的：先判断官方 pinned Host L2 上的搬运性能，同时记录真实 HiCache 加载的管理阶段。无 Mooncake、MF、L3、模型权重或推理。使用当前 checkout 的 SGLang 源码，单进程/单 NPU/TP=1，临时文件初始化单 rank Gloo，不占用 rendezvous TCP 端口。

## 测什么

实际构造 `NPUMLATokenToKVPool`、`HiRadixCache`（内部构造 `MLATokenToKVPoolHost` 和 `HiCacheController`）及 `PagedTokenToKVPoolAllocator`。Host 使用官方 `torch.empty(..., pin_memory=True)` 分配，检测实际 pinned 状态，不回退到 ADXL 或普通 CPU 内存。`model_path=dummy` 仅跳过模型参数解析，未替换缓存实现。

| 输出路径 | 计时范围 |
| --- | --- |
| `copy_whole` | 地址预备后，将整个请求交给相同加载接口，一次同步 |
| `hicache_load` | `match_prefix` → `init_load_back` → `ready_to_load_host_cache` → 等真实完成事件 → `loading_check` |

`copy_whole` 经 `load_to_device_per_layer(..., layer_id=0, io_backend=kernel_ascend)` 调用 SGLKernel；Ascend MLA 在第 0 层调用时搬运全部层。`hicache_load` 由真实 controller 遍历层接口及记录事件，不改变生产代码。

默认模拟 DeepSeek V3.1 MLA：61 层、BF16、128 tokens/page、压缩 KV 512 + RoPE 64。只测连续地址。所有组的 Host/NPU 池、有效页映射和数据相同；第 0 个 NPU page 保留，验证不被覆盖。Host 池按官方分配器从第 0 页分配，不强行模拟旧实验的 Host 保留页。

每组每轮前清零目标 NPU，重置分配器及树，构造一个完整 Host-only 前缀。Host KV 根据 page/layer/token/component 生成；每轮计时后逐页逐字节校验。构造、清零、校验和原始样本写盘均不计时。预热和采样按整个请求执行，两组轮换顺序，默认预热 2 次、采样 10 次。128-token 冒烟各执行一次，不用于稳定性判断。

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

**默认命令一次完成全部测试**：128-token 冒烟通过后自动测全部五档，每档输出两组结果并实时保存。无需手工切换 tokens。`--suite` 是同样行为的显式写法。成功标志为 `L2_ALL_OK`。

只检查冒烟或重跑某档时，可使用：

```sh
python3 workspace/hicache_l2_bench/run.py --smoke
python3 workspace/hicache_l2_bench/run.py --tokens 1024
```

旧版一页冒烟已由用户回报通过；本次整任务/默认全量入口改动仍需远端验证。

套件先冒烟，再测 1K/4K/16K/64K/128K，每档独立进程。128K 的 Host 和 NPU KV 池各约 8.59 GiB（不含库额外资源）；比旧的有限页 L2 测试占用更多 Host 内存。默认每档超时 1800 秒，是防挂死上限。可传 `--device N --warmup 2 --repeats 10 --timeout 1800`。

失败停止后续规模。诊断只需一条命令：

```sh
python3 workspace/hicache_l2_bench/run.py --diagnose
```

回报 `L2_FAIL` 行及诊断输出的最后 35 行。结果保存于本目录 `results/<timestamp>/`：`cli.log`、`status.json`、各档日志/原始 JSON/CSV、`summary.csv`。有效带宽为十进制 GB/s；默认 10 样本的 nearest-rank P95 是最大值。JSON 记录提交、导入源码位置、库版本、实际池容量和 pinned 状态；CANN/驱动/镜像版本应随实验回报，不能从 torch 版本推断。

退出或 Ctrl+C 时 runner 回收自己启动的子进程组；无常驻服务要手工停止。每档进程结束释放内存。失败日志保留，不自动安装或修改环境。

## 本地检查（无需 torch/NPU）

```sh
python3 -m unittest discover -s workspace/hicache_l2_bench -p 'test_*.py'
python3 -m py_compile workspace/hicache_l2_bench/bench.py workspace/hicache_l2_bench/run.py
```
