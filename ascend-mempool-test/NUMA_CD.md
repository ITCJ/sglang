# TP1 / TP2 / TP4：C 大块写入与 D sparse 读取

入口为 `scripts/run_numa_cd.sh`。P/D 各运行一条命令，自动执行七组布局预检查、
三轮性能测量、正确性检查、逐用例保存、结果汇总和自身进程清理。无需模型或服务。
只测 Graph、block_dim=48；这是 C/D 组件实验，服务性能和 NPU 验收另行记录。

## 布局与负载

| 组名 | 并发 rank | 本机物理池数 | 各 owner 的目标 NUMA |
|---|---:|---:|---|
| 1-I0 | 1 | 1 | 0 |
| 2-I0 | 2 | 2 | 0、0 |
| 2-ID | 2 | 2 | 0、2 |
| 2-S0 | 2 | 1 | 0 |
| 4-I0 | 4 | 4 | 0、0、0、0 |
| 4-ID | 4 | 4 | 0、2、4、6 |
| 4-S0 | 4 | 1 | 0 |

两侧使用同一矩阵。共享池由 member-0 分配，其他 member 贡献零物理内存并直接
映射 owner，owner 不转发数据。独立模式的远端映射连接相同 member 的远端副本。
TP1 仅启动每侧一个真实 worker，不靠其他 worker 空跑来模拟。

每个物理池都为 1 层、16 slots、每 slot 65536 行、每行 576 BF16 = 1152 bytes。
有效容量为 1.125 GiB，加 probe 并按 GiB 对齐后，owner 实际贡献 2 GiB。
4-I0/4-ID 的每侧总贡献为 8 GiB，4-S0 为 2 GiB；虚拟地址保留量单列。
各组顺序执行并释放，物理内存不会按整张矩阵累计。

所有组固定使用 NPU 0/1/2/3，TP1/TP2 取相同列表的前缀。
CPU 固定在 node 0 的允许 CPU 集合，分成四个互不重叠的子集；每个 rank 始终
使用对应子集，即使只有 TP1 或池分配到其他节点。CPU pattern 生成最多用四线程。
`bindings.json` 和 worker 报告保留实际 CPU 绑定、NPU ID、SDK、驱动和可见设备环境。
`--numa-nodes` 可替换内存节点列表；I0/S0 使用列表首项，ID 使用前 N 项。

**C 主实验固定逻辑任务 L，大小为 2048 / 8192 / 32768 行，即 2.25 / 9 / 36 MiB。**
独立模式每 rank 向自己的池写完整 L，合计 N×L；共享模式每 rank 写 L/N，
连续、互斥、无重复目的地，整组共写 L。只把 copy 放入 Graph。
源 HBM 数据、地址分片、初始化、CPU 校验都在计时区外。

另测 `write-equal_total-*`：两种模式都让每 rank 写 L/N，整组总 copy 量都为 L。
独立池此时仅更新自己的对应分片，作为等实际字节数的控制，不代表完整副本更新。
TP1 两种定义相同，不重复测。2049 行奇数尾部只做正确性检查。
实际写入区域前后留 guard，报告里的 L 不计 guard。

**D 每请求从 65536 行中取无重复 top-k=2048；batch=1 / 16。**
同一请求的所有 rank 使用同一索引和顺序，每 rank 读取完整结果到自己的 HBM。
每 rank 为 2.25 / 36 MiB；TP4 的合计读取量为 9 / 144 MiB。
共享读取同一物理 KV，独立读取各自内容相同的副本。

分别测本地、远端、mixed（每请求两侧各 1024 行）。一次只启动一侧的消费组，
对侧保持池可用，计时期间不提交设备读写。下一批切换确定性的随机索引及 slot；
同轮同负载在不同布局使用相同索引。64 次 replay 内重复相同索引；每次都发起真实
copy，无应用层 selected KV 缓存复用，不声称硬件缓存处于冷态。
`--contiguous-control` 额外加入连续索引诊断，默认不跑。

## 直接运行

沿用现有两台 NPU 容器中的 torch / torch-npu / UniDexCopy / MemFabric / CANN。
脚本自动 source 既有 Ascend 环境，不安装或修改 SDK。两侧先拉取同一代码。
目标节点 0、2、4、6 需要在线；CPU node 0 至少有四个允许 CPU。
测试期间预留本机四张 NPU 和两侧端口 19400–19438；不要与旧共享测试并跑。

P（10.120.72.31），先启动后等待 D：

```bash
cd /home/cryang/sglang
git pull --ff-only
bash ascend-mempool-test/scripts/run_numa_cd.sh \
  --role prefill --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --devices 0 1 2 3 --numa-nodes 0 2 4 6 --cpu-node 0 \
  --run-id cd-v1-full --timeout 3600
```

D（10.120.72.32）：

```bash
cd /home/cryang/sglang
git pull --ff-only
bash ascend-mempool-test/scripts/run_numa_cd.sh \
  --role decode --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --devices 0 1 2 3 --numa-nodes 0 2 4 6 --cpu-node 0 \
  --run-id cd-v1-full --timeout 3600
```

完整命令包含预检查，不需要手工切换 TP 或 NUMA。
默认三轮，每组合 20 个采样批次、每批 64 次 replay、5 次 warmup。
两侧共 162 个组合，三轮 486 次组合执行，另有 7 组预检查。
控制台显示整体进度、当前轮次/布局/负载、elapsed 和动态 ETA，最多每 10 秒刷新。
刚开始的 ETA 只有预检查数据，随采集逐步更新；`--timeout` 是每个布局阶段上限。

可选模式（两侧添加相同参数，使用新的 run-id）：

- `--preflight-only`：仅七组分配、映射、probe、follower 重入和 NUMA 证据检查。
  仍按真实池容量分配，但不初始化整池 KV，也不跑性能矩阵。
- `--smoke`：仍用 Graph/48、固定池形状与 top-k=2048，缩为一轮、两个采样批次，
  仅 2048 行写入、batch=1 读取；64 次 replay 保留启动偏差检查所需的窗口。
  这是 smoke，不是完整性能结果。
- `--read-sources local`：仅保留本地读取；写入不变，三轮两侧共 318 次组合执行。
- `--describe`：只打印精确配置、布局、容量和执行顺序。需要 CPU torch 等现有
  Python 依赖，不加载 NPU 库、不分配池、不联网。可以不传 role/run-id。

## 结果与计时

日志：`/home/cryang/log/numa-cd/<run-id>/{prefill,decode}/`。
每次运行使用新 run-id，旧目录不会覆盖。

| 文件 | 内容 |
|---|---|
| `summary.json` | 本侧完整结果、原始聚合批次、物理贡献和 NUMA 证据 |
| P 侧 `decode-summary.json` | 自动收集的 D 侧结果 |
| P 侧 `combined-results.csv` | 两侧全部布局/负载的 P50/P95、有效带宽 |
| `results.csv` | 本侧可直接做图的结果 |
| `partial-summary.json` | 已完成布局的中间结果；不代表完整验收 |
| `progress.json` | 进度、当前用例及 ETA |
| `round-N/<layout>/member-M/cases/*.json` | 通过前后校验的逐用例 checkpoint、每批原始数据 |
| `round-N/<layout>/member-M/attempts.jsonl` | 启动偏差不合格的尝试也保留 |
| `round-N/<layout>/member-M.log` / `member-M/report.json` | 详细日志、版本、映射和失败信息 |

主图使用**同机整组** `max(done)-min(start)` 除以 replay 数的 P50/P95；
host 指标包含 Graph 提交、执行和完成等待，不是剔除提交开销后的纯设备计时。
每 rank 的设备 event 时间单列；不同 NPU/主机的设备时钟不相减。
P50/P95 是**批次均摊时间**的分位数，不是单次 replay 或模型请求尾延迟。

合计有效 copy 带宽 = 总有效 copy bytes / 组耗时；逻辑写入速率 = L / 组耗时。
不把有效带宽称为硬件总线实测流量。跨 TP 的读取总量按 rank 数增长。
每批要求同侧 rank 的 host 提交/完成区间重叠，启动偏差不超过最短设备批次的 10%。
不合格批次最多重试三次，仍不合格则失败，不掺入汇总。
原始时间戳和各 rank event 时间用于检查并发提交；此检查不声称测得每条 DMA 的重叠区间。

`numa_verified=false` 保留为待验证：仅 API 请求 node 成功或驱动日志出现 node，
不能证明物理页落点。可观测页出现在错误节点时立即失败；驱动映射没有页计数时
允许测量，但结果只能解释为“指定此分配配置”，不能下 NUMA 因果结论。

| 最终状态 | 退出码 | 含义 |
|---|---:|---|
| `COMPONENT_CHECKS_PASSED` | 0 | 矩阵、校验及可观测 NUMA 检查完成，仍待用户验收 |
| `COMPONENT_RESULTS_PENDING_NUMA` | 2 | 性能结果完整，但实际 NUMA 落点无法核验 |
| `PREFLIGHT_PASSED` / `PREFLIGHT_PENDING_NUMA` | 0 / 2 | 仅预检查结果 |
| `SMOKE_PASSED` | 0 | 仅 smoke 通过，查看单独的 NUMA 状态 |
| `FAILED` | 1 | 原因见 `failure.json` 与对应 worker 日志 |

## 收尾与进度

每组最后按全员设备 drain → 全员 pool leave/destroy → 非 Store SDK 客户端
uninitialize → Store 所在 worker 最后 uninitialize 的顺序收尾。共享模式的
Store 在 P member-0；独立模式各 P member 各自承载对应的 Store。
SDK Store 不能在其他成员仍需 CAS/QueryAlive 完成退出时先关闭。

worker 日志用 `CLEANUP` 标记收尾阶段，`report.json` 的 `cleanup_phase` 同步记录；
父进程显示 `cleanup/...` 或 `waiting-peer-close` 并持续刷新已完成数和 elapsed。
收尾不显示测量 ETA。从本机首次观察到收尾开始，超过 120 秒仍未完成则明确失败，
随后终止并清理本次进程组（最多额外等待约 30 秒）。收尾超时不会算成成功验收。

## 失败后续跑

每个用例通过前后校验后原子保存。两侧所有 rank 都有完整 checkpoint 的用例才复用；
未完成用例重测，已经完成的部分不用重跑。新进程仍执行七组预检查及每组尾部校验。
报告保留来源路径，旧结果的 pending NUMA 不会因新分配的证据而升级。

在原完整命令中，把 run-id 改为 `cd-v1-resume`，再加：

```bash
--resume-from cd-v1-full
```

两侧都这样操作，保留原代码、设备、NUMA、CPU 绑定、负载、随机种子、轮次和采样参数。
通常代码 hash / commit、配置或运行环境改变时拒绝复用。本次启动窗口及收尾修复有受限例外：
允许原始提交 `61e22c8603` 和启动窗口修复 `0d000b262a` 的精确 source hash 续跑到
经过 runtime hash 锁定的修复版。修复涉及启动目标过期、计时结束后的 SDK 收尾顺序
和进度刷新；计时、数据路径和采样验收条件未改。
活跃侧启动过晚仍由原有重叠 / 10% 启动偏差检查拒收并重试，闲置侧晚到不再终止整组。
复用记录保留原 `contract.code` 和来源路径；任何其他运行时代码修改都禁用跨版本例外。
配置、运行环境、全 rank 完整性及 NUMA 证据仍逐项检查。不能复用旧 `run_numa_sharing.sh`
结果，也不能把不同 NUMA 配置的数据混在同一组。超时和日志输出位置不是性能负载。
失败后回传两侧 `failure.json`、最后失败 worker 的日志和 `report.json`；
完成后回传 P 侧 `summary.json`、`decode-summary.json` 和 `combined-results.csv`。

实现只位于独立测试目录。新增路径为 `cd_plan.py`、`cd_copy.py`、`cd_benchmark.py`、
`cd_results.py`、`cd_progress.py` 和运行入口；复用既有池映射、cohort 和进程清理。
每个用例在所有 rank 完成后校验、恢复规范数据；全组 drain 后才关闭池。
Mac CPU 检查不等于 NPU Graph、跨机 copy 或 NUMA 硬件验收。

`cd-v1-resume` 若卡在旧版 SDK 退出中，先在两侧原启动终端按 Ctrl+C，等待本次
进程清理退出，再拉取修复。保留两个旧目录，以覆盖更完整的 `cd-v1-full` 为来源，
用新 `--run-id cd-v1-resume2 --resume-from cd-v1-full` 续跑，其余参数保持一致。
