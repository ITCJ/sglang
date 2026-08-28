# Dense Attention 三层 KV 实验计划

## 1. 验证命题

1. 在目标互联上，`L3 -> L2 -> L1` 命中比 `L2 -> L1` 命中慢。
2. 固定 Host KV 物理预算时，增大私有 L2 虽提高本地命中，却会压缩共享 L3，并因 L2/L3 同时保留相同 KV 降低唯一 KV 容量。
3. locality-first 与 load-first 路由分别偏向缓存命中和负载均衡，三层架构难以同时取得两者。

这里只研究 dense prefix KV；DSA Top-K 的粒度问题另做 sparse 实验。

## 2. 固定环境

- 机器：两台 Ascend 910C 服务器。先记录 HCCS/RDMA、NIC 和 NUMA 拓扑；若不是超节点，结果只代表当前机器。
- 模型：固定 revision 的 `sgl-npu/DeepSeek-V3.1-w8a8`，使用 dense MLA（全量访问 KV），KV dtype 固定为 BF16。
- 部署：每台一个 SGLang replica，每个 replica 使用 `--tp-size 16 --dp-size 16 --enable-dp-attention`；`attn_tp_size=1`、`DCP=1`。实验 0 只启动一台推理服务器，L3 可部署在另一台机器。
- 固定参数：`--attention-backend ascend --page-size 64 --max-running-requests 128`。64K 配置使用 `--max-total-tokens 73728`；128K 配置在预检查通过后使用 `--max-total-tokens 139264`。
- HiCache：`write_through`、Mooncake RDMA、L3 单副本、`wait_complete`。NPU 启动日志必须确认实际为 `kernel_ascend + page_first_direct`。
- 打开 `--enable-metrics --enable-cache-report`；客户端使用 `--cache-report --output-details`。
- 每个配置从空 L1/L2/L3 和空 Router tree 开始，独立运行 3 次；报告 3 次中位数及最小/最大值。

除实验指定项外，模型、L1、请求内容、请求顺序和调度参数不变。

使用 DP Attention，并在每个 replica 内将同一 prefix 固定到唯一 DPA rank，以避免 rank 维度的人为 KV 复制成为混杂变量。实验 2 允许 Router 将同一 prefix 分配到不同 replica；本计划不比较普通 TP 与 DP Attention。

### 启动校准与容量口径

每次启动记录各 DPA rank 的模型权重占用、L1 token 容量和 HiCache 实际 `size_per_token`。L1 容量用于确定实验 0 的淘汰工作集，并确认实验 1 的 L2 大于 L1；`size_per_token` 用于计算实验 1 的 page bytes、工作集和有效容量。

按当前模型配置，BF16 MLA KV 的理论值为：

```text
61 * (kv_lora_rank 512 + qk_rope_head_dim 64) * 2 Bytes
= 70,272 Bytes/token/rank
```

因此一个请求的 KV 约为：64K 输入 `4.6 GB/rank`，128K 输入 `9.2 GB/rank`。该理论值仅用于启动校验；所有容量计算以启动日志的实测值为准，并记录不一致的原因。

## 3. Workload

| 实验 | 类型 | 内容 |
| --- | --- | --- |
| 0 | 受控合成 | 64K 主长度、128K 压力长度，精确制造 production path 的 L2 hit 与 L3 hit |
| 1 | 受控合成 | 64K 输入的固定 Zipf prefix 请求序列 |
| 2 | trace-driven 半合成 | Mooncake `conversation` 的时间戳、`hash_ids`、输出长度 |

实验 2 的 prompt 文本和 prompt 长度由仓库根据 `hash_ids` 重建，只有时间戳、复用标识和 `output_length` 来自 trace；它不是生产请求原样重放。

## 4. 实验 0：L3 命中的额外路径开销

### 目的

测量当前三层实现中，同一 prefix 从 L3 命中相对从 L2 命中增加的真实请求延迟。

### 方法

1. 单 replica、TP16+DPA16，L2=`20 GB/rank`，Mooncake store=`640 GB`；驱动程序只调用生产 `/generate` API，并将全部请求固定到 `routed_dp_rank=0`。
2. 主实验固定 10 个互异的 64K 输入；每个输入由 `65,408-token prefix + 128-token question` 构成，`max_new_tokens=1`、`concurrency=1`。若 128K 预检查通过，再以 `130,944-token prefix + 128-token question` 重复相同流程。
3. **L2 hit**：先生成全部 prefix 并等待 write-through 完成，再写入足量互异 filler 淘汰 L1。filler 数量由启动日志中的 L1 token 容量决定，至少为 `1.2 * L1_capacity`。仅接纳 `device=0、storage=0`，且 `host` 覆盖全部可缓存完整 page 的请求。
4. **L3 hit**：确认 prefix 已写入 L3 后重启 worker、保留 Mooncake，使 L1/L2 为空。仅接纳 `device=0、host=0`，且 `storage` 覆盖全部可缓存完整 page 的请求。
5. 对同一 prefix 的 L2 hit 和 L3 hit 做配对比较；每个配置独立运行 3 次。

### 指标

- 主指标：`TTFT = first_nonempty_token_time - HTTP_send_start`。
- 配对差：`DeltaTTFT_j = TTFT_L3,j - TTFT_L2,j`。
- 可选 breakdown：复用现有计时记录 `T_32`（`batch_get_v1`）和 `T_21`（Host-to-Device load），不新建 micro benchmark。

每个 run、每个长度先在 10 个 prefix 上计算 `median(TTFT_L2)`、`median(TTFT_L3)` 和 `m_r=median(DeltaTTFT)`，再报告 3 个 run-level 值的中位数及最小/最大值。

64K 的 3 个 run 若都满足 `m_r>0`，说明当前三层实现的 L3 hit 存在可重复的额外延迟。该实验不区分实现问题与设计问题，也不证明 L2 必然多余；它只建立当前三层实现的端到端基线，供未来与优化后的新系统使用相同 workload 对比。128K 只用于观察差值是否随 KV 量继续放大。

## 5. 实验 1：命中率与有效容量

### 目的

验证增大 L2 的收益和代价：

- 收益：更多请求从 L2 命中，TTFT 降低。
- 代价：KV 副本增多，相同内存能保存的不同 KV 减少。

### 方法

1. 每台机器最多使用 `1.2 TB` Host 内存，保留约 `600 GB` 给系统、运行时和测量工具。两台合计固定物理预算 `B=2.4 TB`（`1 GB=10^9 bytes`）。每台一个 TP16+DPA16 replica，测试三种 L2/L3 划分：

| 配置 | `--hicache-size`/rank | L2/台 | `global_segment_size`/replica | L3/台 |
| --- | ---: | ---: | ---: | ---: |
| 小 L2 | 20 GB | 320 GB | 880 GB | 880 GB |
| 中 L2 | 40 GB | 640 GB | 560 GB | 560 GB |
| 大 L2 | 60 GB | 960 GB | 240 GB | 240 GB |

`--hicache-size` 和 Mooncake 配置均使用可读的 GB 值；执行脚本负责转换，不在计划中手写长整数。`B_actual` 定义为启动日志中两台机器实际分配的 L2 与 L3 字节数之和。若任一配置无法分配或注册内存，三种配置按相同比例缩小。每档 L2 都必须大于实测 L1；否则先缩小 L1 token 容量，而不是改变三档总预算。

2. 一条请求为“65,408-token 共享 prefix + 128-token 独立问题 + 1-token 输出”，即输入恰好 64K。使用仓库的 `generated-shared-prefix` 生成固定 Zipf 请求文件：

```text
--dataset-name generated-shared-prefix
--gsp-num-groups <N_group> --gsp-prompts-per-group 16
--gsp-system-prompt-len 65408 --gsp-question-len 128 --gsp-output-len 1
--gsp-group-distribution zipf --gsp-zipf-alpha 1.2 --seed 1
--warmup-requests 0 --max-concurrency 1
```

生成后统计文件中实际出现的不同 prefix 数量，并根据实测 `page_bytes` 计算工作集 `W_unique`；增加 `N_group` 直至 `W_unique >= 1.2*B_actual`。随后固定请求文件及其 SHA256，三种配置使用完全相同的请求和顺序。

每个 prefix group 通过固定哈希映射到唯一的 `(replica, routed_dp_rank)`，三个配置保持映射不变。这样同一 prefix 不会因普通 TP 或 workload 人为广播产生跨 rank 副本；容量统计只保留 L2/L3 层级策略造成的重复。

3. 实验 1 不经过 Router；客户端按固定映射直接请求对应 replica，并在请求体中设置 `routed_dp_rank`。请求串行发送。每个配置从空缓存开始：第 1 个 epoch 填充缓存，第 2 个 epoch 测量；第 2 个 epoch 完成并等待 worker idle、write-through 完成后，立即取得 L2/L3 终态快照。每个配置独立运行 3 次。

### 指标

第 2 个 epoch 统计：

- `H_L2=sum(host_cached_tokens)/sum(prompt_tokens)`。
- `H_L3=sum(storage_cached_tokens)/sum(prompt_tokens)`。
- `TTFT_P95`：成功请求 TTFT 的第 95 百分位。

终态快照只统计完整 KV page：

- 测量结束并等待 idle/write-through 后，增加一个只读调试接口：遍历 HiRadix tree，按 rank 导出仍有 `host_value` 的 L2 page hash；再用本轮出现过的全部 page hash 对 Mooncake 做一次批量存在性查询，得到 L3 page hash 集合。
- 令 `b_page=page_size*size_per_token`。`P_end=b_page*(各 rank 的 L2 page 数之和+L3 page 数)`，即实际保存的所有物理页。
- `U_end=b_page*|union(所有 L2 page hash, L3 page hash)|`，即按 page hash 去重后实际保存了多少不同 KV。
- 例如 L2 有 `{A,B}`、L3 有 `{A,B,C}`，则 `P_end=5*b_page`，`U_end=3*b_page`。
- 填充率 `rho=P_end/B_actual`。
- 有效容量率 `eta=U_end/B_actual`。
- 副本放大 `A_copy=P_end/U_end`。

`rho>=0.9` 表示 L2+L3 的可用页槽至少填满 90%，容量差异才真正受到预算限制。若未达到，增加 `N_group` 后重跑。prefix 到 DPA rank 的固定映射已在发送请求时保证，不再作为额外判定条件。结果表只列：`L2/L3、H_L2、H_L3、TTFT_P95、rho、eta、A_copy`。

## 6. 实验 2：Locality 与 Load Balance

### 目的

验证缓存 locality 和负载均衡是否存在冲突：locality-first 是否提高本地命中，却造成更不均衡的请求负载，并影响吞吐或 TTFT。

### 方法

1. 固定实验 1 的中 L2 配置。将 `conversation_trace.jsonl` 按 `timestamp` 稳定排序，取前 2500 条：前 500 条 warmup，后 2000 条 measurement。只使用 trace 自带的到达时间、`hash_ids` 和输出长度，不人工制造热点。

```text
--dataset-name mooncake --dataset-path <warmup-or-measurement.jsonl>
--mooncake-workload conversation --mooncake-num-rounds 1
--num-prompts <500-or-2000> --mooncake-slowdown-factor <s>
--warmup-requests 0 --max-concurrency 2500 --cache-report --output-details
```

2. 每个策略都从空缓存开始，先重放 warmup，等待 idle/write-through，再保持进程不变重放 measurement。Mooncake dataset 按 trace 时间发送；客户端额外记录 `planned_send_ns` 和 `actual_send_ns`。

3. 三个策略都使用 `routed_dp_rank=stable_hash(prefix_id) % 16`，使相同 prefix 在一个 replica 内落到同一 DPA rank。Router 只选择 replica。

4. 先用 hybrid 对 measurement 做一次零间隔发送，测得饱和吞吐 `X_sat`。统一缩放 trace 时间，使正式到达率为 `0.85*X_sat`。

5. Router 固定 `--worker-urls <worker0> <worker1> --disable-retries`，只改变以下策略参数：

| 策略 | `...` 中的参数 |
| --- | --- |
| load-first | `--policy cache_aware --cache-threshold 1.0 --balance-abs-threshold 0 --balance-rel-threshold 1.0` |
| locality-first | `--policy cache_aware --cache-threshold 0.3 --balance-abs-threshold 2500 --balance-rel-threshold 1.0` |
| 当前 hybrid | `--policy cache_aware --cache-threshold 0.3 --balance-abs-threshold 64 --balance-rel-threshold 1.5` |

`cache-threshold` 是 Router 根据历史请求文本估算的 prefix 匹配率阈值，不是 L1/L2/L3 的实际命中率；只有匹配率严格大于该值时才按 cache locality 选择 worker。`cache-threshold=1.0` 使 load-first 选择最小 active load；`balance-abs-threshold=2500` 使 locality-first 在有足够 prefix match 时优先 locality。命中结果以 SGLang cache report 为准。

Router 只增加一个 `decision_total{reason}` counter：正常分支为 `cache_match`、`low_match_min_load`、`imbalance_min_load`，tree 缺失或 stale tenant 等路径统一记为 `other_fallback`。

### 指标

测量窗口 `[t0,t1]` 定义为首个 measurement 请求开始 HTTP send 到最后一个响应完成；只统计这 2000 个请求：

- 令 `D_r=Delta decision_total{reason=r}`，`D=sum_r D_r`；`F_cache=D_cache_match/D`。
- `H_local=sum(device_cached_tokens+host_cached_tokens)/sum(prompt_tokens)`。
- `H_L3=sum(storage_cached_tokens)/sum(prompt_tokens)`。
- `R_L2,end=P_L2,end/U_L2,end`：`t1` 后等待 idle 30 秒，按实验 1 的对象定义取得一次 L2 快照。
- 每 100 ms 抓取 `a_i(t)=smg_worker_requests_active{worker=i}`，用梯形法计算 `A_i=(1/(t1-t0))*integral[a_i(t)]dt`；`I_active=max_i(A_i)/mean_i(A_i)`，1 表示均衡。
- 全局请求吞吐 `X=2000/(t1-t0)`，单位 req/s。
- `TTFT_P95`：定义同实验 1。

令 `lag_i=(actual_send_ns-planned_send_ns)/1000000` ms；将 2000 个 lag 升序排列，P99 取索引 `ceil(0.99N)-1`。有效 run 必须满足：lag P99 不超过 10 ms、无客户端 semaphore 等待、2000 个请求全部成功、`D=2000`、`D_other_fallback=0`，且 locality-first 的 `F_cache>0`。

结果表只列：`policy、F_cache、H_local、H_L3、R_L2,end、I_active、req/s、TTFT_P95`。不测 scheduler queue、NPU 利用率或运行 batch。

## 7. 判定

- 实验 0：L3 hit 的配对 TTFT 在 3 次独立运行中都高于 L2 hit，说明当前三层实现存在可重复的 L3 命中额外延迟。
- 实验 1：随 L2 增大，`H_L2` 上升、TTFT 下降，同时 `eta` 下降或 `A_copy` 上升，支持低延迟命中与有效容量的权衡。
- 实验 2：相对 load-first，locality-first 提高 `H_local`、降低 `R_L2,end`，但提高 `I_active`，并降低 req/s 或提高 TTFT，支持 locality 与 load balance 的权衡。若实际路由选择没有差异，则该 trace 不提供此命题的证据。

未来系统只与调优后的三层实现比较，并保持模型、trace、L1 和总物理内存预算相同。
