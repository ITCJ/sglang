# A3 / 910C：index K offload 的第一阶段可行性测量

仅在目标昇腾 A3（910C）机器/容器执行。本目录在开发机只做静态检查，不运行模型、测试、profiler 或 microbenchmark。

目标：分别测 **BS=11、2K 输入时的稳定 decode 计算窗口**，以及 **BS=11、64K 历史时单层 index K 的 DRAM→HBM 时间**。本轮没有真正的 index K offload，也没有计算与传输竞争实验。

## 文件

| 文件 | 职责 |
| --- | --- |
| `common.sh` | 模型路径、BS、输入/输出长度、端口、Ascend 环境路径 |
| `launch_server.sh` | 单机 TP16 colocation 服务；NPU graph 与 profiling 参数；保存启动命令和日志 |
| `preflight.py` | 目标机加载模型前检查依赖导入、16个可见逻辑NPU、模型维度和已安装CLI参数，保存版本与检查结果 |
| `profile_decode.sh` | 创建独立目录并启动 profiling 客户端 |
| `profile_decode.py` | 等全部请求 decode warmup 后手动开始/停止 profiler，记录窗口前后 token 计数并检查提前结束 |
| `run_transfer_bench.sh` | 启动一个或 16 个 worker，支持按 rank 绑定 NUMA |
| `transfer_bench.py` | 单层完整 index K 副本的连续 pinned H2D 拷贝；输出各 rank 和最慢 rank 统计 |

参考：`../col.sh`、`../kv_path_bench/kv_transfer_bench.py`、`../hicache_l2_bench/bench.py`；profile 的 `/start_profile → 短窗口 → /stop_profile` 顺序来自 [指定参考脚本](https://github.com/ITCJ/usefulScript/blob/main/ascend_env/profile_sglang.sh)。API 参数按本地 SGLang `31df8e91a` 核对。目标机的 SGLang、CANN、torch-npu 版本不同可能需要调整参数。

## 默认 BS 与容量口径

采用 `K=1024 tokens`，用户的 741K/22.8M 容量估算分别暂记为 741,000 / 22,800,000 tokens：

```text
原 index K 容量限制：floor(741000 / 65536) = 11 requests
host 主 KV 容量上限：floor(22800000 / 65536) = 347 requests
```

本轮按要求测 **BS=11**，并非 BS=347。741K 若使用二进制单位、64K 若按 64,000，向下取整仍是 11。该 BS 未给长上下文生成预留很大的余量；这里只用于选择计算负载。

假设 DSv3.2 为 61 层，主 KV 每 token 每层 `(512+64)×2=1152 B`，index K 每层 `128×2=256 B`。启动脚本明确使用 BF16 cache；W8A8 是权重量化，不用于推断 cache 字节数。

| BS=11 的计算实验 | 主 KV / rank | index K / rank | 两者合计 / rank |
| --- | ---: | ---: | ---: |
| 2048 输入 tokens | 1.583 GB | 0.352 GB | 1.935 GB |
| 2048 输入 + 最多512输出 | 1.979 GB | 0.440 GB | 2.419 GB（约2.25 GiB） |

主 KV/index K 在该普通 MLA TP 路径中不能再除以 TP16。以上只计 cache payload，不计分页额外槽位、权重、activation、workspace 和 graph；cache payload为 **28,160 tokens**，客户端还按每请求额外一页检查 admission 余量，若page size=128则要求至少29,569 tokens。2.419 GB 明显低于按 741K×61×256 推回的 11.57 GB index-only 预算，因而短上下文方案在该口径下有容量余量，但仍以实际启动和请求是否 retract/OOM 为准。

默认 `context-length=2048+512+256=2816`。不能只设成2560：本地worker的 `max_req_len=context_len-1`，scheduler又将输出裁剪为 `max_req_len-input_len-1`，设2560实际只能输出510，导致客户端的512-token完整性检查失败。额外context上限不会改变实际2048-token输入。

微基准一层的默认传输量为：

```text
11 × 65536 × 128 × 2 = 184,549,376 bytes = 176 MiB / rank
```

16 个 rank 各搬完整副本，每次合计 2.75 GiB。默认每 rank 两个轮转 host buffer、一个 device buffer，合计约 **5.5 GiB pinned DRAM + 2.75 GiB HBM**（分布于16个逻辑设备），不是申请全模型61层的 buffer。`--layers 61` 仅用于流量统计。

## 目标机运行：计算 profiling

在与现有 `col.sh` 相同的已安装 SGLang/NPU 依赖的容器中运行。默认模型路径沿用 `col.sh`；根据实际挂载修改。下面两终端需要使用一致的环境变量和本目录。

终端一：

```bash
cd /nfs2/yhc/huawei/sglang/workspace/index_k_overlap
export MODEL_PATH=/home/caofei/DeepSeek-V3.2-Exp-w8a8
export BS=11 INPUT_LEN=2048 OUTPUT_LEN=512
bash launch_server.sh
```

启动会先执行 `preflight.py`，只检查依赖和CLI，不加载权重；失败时查看终端提示的 `logs/server_*.log`，以及结果目录的 `preflight.json` / `server_cli_help.txt`。通过后才加载模型。确认没有其他请求流量、实际使用单机16个逻辑 NPU。

终端二：

```bash
cd /nfs2/yhc/huawei/sglang/workspace/index_k_overlap
export MODEL_PATH=/home/caofei/DeepSeek-V3.2-Exp-w8a8
export BS=11 INPUT_LEN=2048 OUTPUT_LEN=512
bash profile_decode.sh
```

客户端依赖 `aiohttp`、`transformers` 和可读的模型 tokenizer。输入为合成自然语言 token 序列，每个请求精确2048 tokens，禁用 radix cache、设置 `ignore_eos=true`，固定最多512个输出，避免 EOS 提前降低 BS。此输入用于计时，不代表真实专家路由分布；后续可更换代表性数据。

### 稳态窗口如何选择

1. 等待服务ready（默认最多1800秒），读取并保存 `server_info.json`，核对模型路径、TP16/DP1、NPU、BF16 cache、请求/上下文/cache容量、非MTP/PD/DCP；然后同时提交11个流式请求，服务限制最多11个 running requests。
2. **全部请求**都已至少输出64 tokens，且没有请求完成时，调用 `/start_profile`。
3. 至少记录1秒，且全部请求在 start-profile 返回后各再推进至少8 tokens，再调用 `/stop_profile`。最多采集30秒，避免固定1秒在较慢机器上采不到足够decode；可用 `--profile-min-tokens` / `--profile-max-seconds` 调整。
4. 继续接收已有请求直到完成，不再补入新请求。日志保存到 `logs/decode_*.log`，结果目录保存 `summary.json`、`progress.jsonl`；若请求在采集/flush阶段结束或发生retraction，标记 invalid 并以非零退出。flush期间结束也保守判invalid，需要查看时间线确认。
5. 检查独立 `steady_decode` 目录内至少有16份非空、大小稳定的 `trace_view.json`，最多额外等待300秒；缺文件不能报告成功。客户端必须和服务使用同一容器/文件系统；该检查按当前NPU profiler布局实现，版本不同导致文件命名变化时会明确报错而非静默通过。

`ctx=2K` 在本脚本中指 **输入2048**；采集时实际历史约为2048+已生成 token 数（至少约2112），不是固定2048。窗口前后每个请求的计数保存在 JSON 中。流式返回只能证明客户端观察到的进度，最终还需检查 trace 与服务日志：窗口内没有 prefill/retraction，实际 batch 恒为11，所有 rank 正常。

需要调整窗口时：

```bash
WARMUP_TOKENS=96 PROFILE_SECONDS=1 bash profile_decode.sh
```

若提示 batch 提前结束，需要缩短采集窗口，或同时提高 **服务和客户端**的 `OUTPUT_LEN` 并重启服务，确保 `CONTEXT_LENGTH >= INPUT_LEN+OUTPUT_LEN+2`（默认额外留256）。不要只改客户端而触发服务截断。每次采集用独立结果目录。

### 与 col.sh 的差异

- 本地量化注册名称为 `modelslim`，修正参考脚本的 `modelsim` 拼写；可用 `QUANTIZATION` 覆盖以适配目标分支。
- 默认启用 graph，capture BS 包含精确的11，避免 padded BS 混淆计算量。原 `col.sh` 注释写图模式，但实际传了 `--disable-cuda-graph`。
- 显式传 `--enable-profile-cuda-graph`。**它控制启动时 graph capture profiling，不会自动采集服务稳态 decode**；后者仍由 API 触发。
- `GRAPH_MODE=0 bash launch_server.sh` 可另跑 eager 对照，用于辅助辨认层/算子；不能把 eager 的耗时当作 graph 基线。eager 下仍传 profile 参数，但不会产生 graph capture trace。
- 保留 MLAPO、多 stream、HCCL 配置、DP attention/LM head 参数与关闭 shared-expert fusion；本地版本在 `dp_size=1` 时会将两项 DP 开关归一化为关闭，以服务实际解析配置为准。不启用 MTP、DCP 或 PD。
- 预填充不分 chunk，`max-prefill-tokens=2048` 限制单次 admission 的预填充负载；只在全部请求 warmup 后采集，因此不把启动爬升当成稳态。
- 不修改系统级 CPU governor/sysctl；目标机采用既有性能配置，并保证不同 case 一致。
- 使用 `SGLANG_PROFILE_V2=0` 的手动 API。没有使用 `start_step`，因为本地 legacy 实现中它是绝对 scheduler counter，不能当作“跳过N步”。

### 输出和计算指标

```text
results/server_<时间>_bs11/
  command.txt
  preflight.json
  server_cli_help.txt
  log_path.txt
  startup_profile/graph_capture_profile/...
results/decode_<时间>_bs11/
  summary.json
  log_path.txt
  server_info.json
  progress.jsonl
  steady_decode/...                    # NPU profiler 导出目录
```

按 torch-npu 版本，trace 位于 `steady_decode` 下对应 worker 的 `ASCEND_PROFILER_OUTPUT/trace_view.json` 等位置。**分析 steady_decode，勿将启动 capture trace 当成运行性能结果**。启动 trace 可辅助识别 graph 中的算子、shape 与层次。

在 Ascend profiler/trace viewer 中选完整、稳定的 decode steps，逐 rank、逐层记录：

| 指标 | 口径 |
| --- | --- |
| `Tstep` | 完整 decode step 的设备时间线跨度，跨多个step看p50/p95 |
| `Tindexer,l` | indexer 投影、评分、top-k，和主 attention 分开 |
| `Tattn,l` | 本层主 attention 阶段跨度；注明是否含投影、KV写入与通信 |
| `Tmoe,l` | 路由、专家计算、必要通信、shared expert 和 combine 的完整跨度；首几层 dense FFN 单独标记 |
| `W_moe,l` | 本层 MoE 可开始至下一层 indexer 消费历史K的实际窗口 |
| `W_attn+moe,l` | 更早预取时的窗口；以实际可复用 buffer 的时间为起点 |

不要把多 stream 重叠的 kernel duration 相加当成阶段 wall time；通信和等待需要计入关键路径。记录慢 rank、层间差异，不能只取一个全模型平均 MoE 占比。64K 的 attention/indexer 不由该2K运行代表；本轮主要借用相同 BS 的 MoE 时间。没有主 KV offload 的2K attention 窗口同样不能直接等同于未来长上下文 offload 窗口。

## 目标机运行：传输 microbenchmark

先停止推理服务和其他 NPU 工作负载，再分别执行，避免引入本轮不打算测的计算竞争。

```bash
cd /nfs2/yhc/huawei/sglang/workspace/index_k_overlap

# 单个逻辑NPU（默认可见设备0）。
NPROC=1 bash run_transfer_bench.sh

# A3 单机16个逻辑NPU同时搬运，每个rank搬完整176 MiB。
NPROC=16 bash run_transfer_bench.sh
```

`LOCAL_RANK` 对应 torch-npu 可见的逻辑设备编号，不要假定“8张物理卡”意味着只能起8个进程。若设置 `ASCEND_RT_VISIBLE_DEVICES`，它必须暴露足够设备；结果会保存该变量和实际 device name。

不预设 A3 的 NUMA 编号/设备亲和性。先用目标机已有拓扑信息、`npu-smi info`、`numactl --hardware` 确定映射，再按 rank 顺序提供 `NUMA_NODES`，例如单设备确认为NUMA0时：

```bash
NUMA_NODES=0 NPROC=1 bash run_transfer_bench.sh
```

16-rank 时传16个逗号分隔的 NUMA node 编号。wrapper 在导入 torch、分配 pinned buffer 和 first-touch 前执行 `numactl --cpunodebind --membind`；无配置时继承现有策略，记录CPU affinity，不声称实现了本地 NUMA 绑定。映射无效直接失败，不静默降级。

可覆盖参数：

```bash
# 默认：warmup 10，采样50次，2个轮转host buffer，一次拷完整层。
NPROC=16 TRANSFER_REPEATS=100 bash run_transfer_bench.sh

# 作为额外敏感性观察：按4 MiB连续分块提交，计入多次提交开销。
NPROC=16 COPY_CHUNK_BYTES=4194304 bash run_transfer_bench.sh

# 若改为其它BS/历史长度，计算和传输结果必须按同一目标case对应。
BS=11 TARGET_CTX=65536 NPROC=16 bash run_transfer_bench.sh
```

多 rank 用 Gloo CPU barrier 对齐每次传输，barrier 不计入单rank拷贝耗时；采样区间没有HCCL流量。分配/初始化、所有buffer的完整字节校验在计时外。每次记录：

- `wall_ms`：主机端从提交到完成的耗时，包含 launch、event 和同步等待，初筛优先用它。
- `event_ms`：NPU stream 上起止event间隔，可能含分块提交间隙，不冒充纯DMA引擎活跃时间。
- 每rank p50/p95和GB/s（十进制）；每轮取最慢rank，再计算p50/p95。

输出为 `results/transfer_<时间>_bs11_ctx65536_n16/{command.txt,log_path.txt,rank_00.json,...,summary.json}`。`summary.json` 的 `slowest_rank_per_iteration_wall` 是跨rank初筛的保守参考，也应保留各rank明细和计算窗口对应比较。聚合GB/s按复制总字节数除以最大rank耗时估算，不包含CPU barrier释放的起始偏差，不是链路物理带宽测量。

此结果是连续 pinned-memory `copy_` 基线，**不覆盖**散页gather、host packing、page-table维护、实际 `kernel_ascend`、Mooncake/ADXL/fabric 注册路径、全61层大工作集或MoE/主KV offload竞争。默认两组buffer可能享受缓存复用，因此只能作为该预分配路径的乐观参考；可增大 `HOST_BUFFERS` 观察工作集敏感性，注意相应host容量增长。多rank测试已经包含各rank传输之间的竞争，比单rank结果更接近TP16，但仍不证明实际overlap成立。

## 审查与验证边界

已按本地源码审查参数、API、streaming metadata、NPU profiler输出路径与多rank计时，补充了上述目标机预检查、容量检查、retraction检查、最小采集进度、trace落盘检查和客户端错误日志。开发机仅运行Shell语法检查与Python AST解析，没有执行这些脚本的工作负载，也没有跑单元测试。

不能承诺未经A3实跑即一次成功：预检查不证明目标模型权重完整、CANN/torch-npu/sglang算子运行时兼容、graph捕获一定成功，或实际HBM/host pinned memory足够。第一次目标机执行仍是硬件验证。输出成功仅说明客户端窗口和trace文件检查通过，设备时间线中的实际BS、完整step和kernel阶段仍需确认；本轮不会自动输出可靠的Tmoe/Tattn汇总。

## 本轮如何判读

整理一行：`BS, target_ctx, actual_compute_ctx_range, bytes/layer/rank, Tload_1rank, Tload_16rank_slowest, Tmoe, W, Tstep`。

优先比较同一rank/层的 `Tload` 与 `W_moe`；聚合摘要可用慢rank加载与较短窗口作保守初筛。若连这个无计算竞争、连续内存路径都明显超出窗口，当前单层lookahead方案缺乏余量；若小于窗口，只能判定“值得进一步测竞争”，不能宣称已经隐藏传输。提前多层与改变布局/传输路径可能改变结论。

参考脚本/API：

- [外部 profiling 参考](https://github.com/ITCJ/usefulScript/blob/main/ascend_env/profile_sglang.sh)
- 仓库 `docs/docs/hardware-platforms/ascend-npus/optimization/profiling.mdx`
- 仓库 `python/sglang/srt/managers/scheduler_components/profiler_manager.py`
- 仓库 `python/sglang/srt/hardware_backend/npu/graph_runner/npu_graph_runner.py`

## 日志管理

三个入口脚本启动后统一重定向 **stdout 和 stderr** 到本目录 `logs/`，不使用 `tee` 刷屏，也不把实验输出日志放入 `/tmp`。环境初始化、preflight、server、客户端、torchrun及worker的输出/traceback均被收集。torchrun自身的日志目录也显式指定在 `logs/` 下。

- `logs/server_bs11_<时间>_<PID>.log`：环境、预检查、服务完整输出。
- `logs/decode_bs11_<时间>_<PID>.log`：profile客户端完整输出。
- `logs/transfer_bs11_n16_<时间>_<PID>.log`：torchrun与各worker完整输出。
- `logs/torchrun_<运行标识>/`：torchrun内部日志目录。

终端仅显示日志路径、运行阶段、完成状态或失败退出码，不自动打印错误全文。需要实时查看时，自行执行 `tail -f <终端显示的日志路径>`。`LOGS_DIR` 可覆盖日志根目录；每次运行有独立时间戳/PID，结果目录的 `log_path.txt` 指向对应日志。profile原始trace、JSON统计、配置快照仍在 `results/` 下。两个目录均被git忽略。
