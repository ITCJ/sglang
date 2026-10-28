# 阶段 0 单请求基线：Ascend NPU

本入口用于 K1 容量改造前后的性能回退对照。它不调优，也不替代 V1–V10 完整功能验收。
本地实现完成后仍需用户在真实 NPU 上运行，基线票才可关闭。

## 配置与运行

两台机器使用相同代码、模型、依赖和已验收的阶段 0 JSON 配置。
配置格式见 [mempool-min-full.example.json](configs/mempool-min-full.example.json)。
把其中 P/D 地址、权重路径和网卡替换为验收时的真实值；以下 `/ABS/PATH/phase0-config.json` 指该文件。
P/D 各一个 TP16 实例，attention TP16/DP1，页128、S_P=8192、S_D=128，prefix每份40页。
脚本继承原 `run_service.sh`：P eager、D Graph BS16、原生 warmup；不改 deterministic 和输出刷新设置。
`perf` 自动打开 matching、关闭 `verify_prefix` 重型回读；原 acceptance 模式不变。

先在 P 容器启动，再在 D 容器启动。每台机器只执行一条命令：

```bash
# P：Main Router 与 P 实例由此命令自动管理
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode perf \
  --config /ABS/PATH/phase0-config.json \
  --run-id baseline01-20261028-001
```

```bash
# D：run-id、配置内容与轮数必须和 P 一致
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode perf \
  --config /ABS/PATH/phase0-config.json \
  --run-id baseline01-20261028-001
```

两端脚本自动加载既有 Ascend toolkit/ATB 环境，检查端口与主机内存。
先跑不计时的独立标准参考，再跑5轮 A→B→C→E。
每轮重启 Main 与两侧 TP16 服务，并等待原生 warmup/就绪，确保 prefix 从空开始。
首轮原始数据保留但不入统计；后4轮每用例每指标至少4个有效样本。
可以在两条命令都加 `--perf-rounds 7` 增加轮数，仍只排除第1轮。
一次完整运行需要加载参考服务及5轮目标服务，模型启动时间不进入请求指标。

测试前应确保指定端口没有其他服务。入口只清理自己启动的进程，不会清理已有服务。
启动前可在任一机器加 `--describe` 查看有效配置、内存预算与阶段，不启动 NPU。
P 的 BM 贡献预算是208 GiB；模型与其他缓冲还需额外资源。具体可用量由 preflight 检查。

## 样本、失败与统计

输入使用 ticket04 的同一逻辑题生成器与 tokenizer，保存完整 IDs。
A/B/C/E 输入长度4096/5120/4096/4096；B精确包含A前4096，C=A，E首页不同。
生成固定 greedy、ignore_eos=true、max_new_tokens=128，无 stop/stop_regex/min_new_tokens。

每条样本检查：输出恰好128 IDs且 length结束，命中0/4096/3968/0，
prefix32/40/40/40，账本与释放正确。参考和实际的核心答案分别与独立标准答案核对。
按用户在 baseline01-001 后确认的 `core-answer-v1` 规则，完整128-token序列不一致只作记录，
不阻塞实验、不使该样本无效，也不跳过 C/E。
逻辑、长度、命中、账本或其他检查失败仍标无效、保存计时；继续尝试后续用例和轮次。
服务启动/进程/双端协调无法继续时仍报告真实执行中断，不伪造未运行的请求。
指标缺事件、跨进程相减、设备步骤不完整、缺拓扑注册、清理失败均不能通过。

逻辑检查只解析输出开头第一个JSON对象（允许前导空白），精确比较 place/pencils/route
三个字段、类型与标准答案；重复字段、缺字段、错误答案或无法解析均标失败。
不在续写中搜索另一个正确答案。强制128-token生成后的续写完整保存，不评价其逻辑正确性。
ABCE标准答案依次为 harbor/22/north、harbor/25/south、harbor/22/north、meadow/27/south。
每条保存完整参考/实际text与IDs、首差索引（从0开始）、首差token和逻辑检查结果。
全部轮次后汇总跨轮完整IDs分组、每轮相对首次输出的首差、逻辑答案分组，以及参考A/C稳定性。
输出稳定性包括预热及无效样本，差异本身只作诊断。
本票不运行 V2 的内容回读、V4 复制适配器、长生成、并发、故障注入或 profiler。

每指标×用例输出n/p50/min/max。P先计算每rank自己的区间，再取16 rank的min/max。
D rank0保存每一步，T_step另报p90/p99/max，第1步单列，并保存实际步数（预期127）。
某指标跨有效轮max/min>1.5而未解释时，报告失败。可以增加轮次，或离线补充有证据的原因。
全部轮次结束后统一报告自动检查状态；存在检查失败、样本不足或未解释的计时波动时返回非零。
服务/清理故障也返回非零，报告与原始日志保留。`execution_complete` 单独表示全轮响应是否已收集。
`status` 仅为自动检查结果，`user_accepted=false` 始终保留，是否通过由用户验收。

## 日志与离线报告

以默认 run-id 为例，P 汇总目录是：

```text
/home/cryang/log/mempool-min/baseline01-20261028-001/prefill/
  baseline.md                主表、A/B拆分、等待、拓扑、稳定性及混淆项
  perf-report.json            机器可读统计、无效原因与样本状态
  output-analysis.json        参考/实际完整输出、核心答案、首差及跨轮稳定性
  samples.jsonl               原始计时；保留预热与无效计时值
  identity*.json / code.patch  双端版本、依赖、源码身份
  topology.json              P的Ascend驱动/CANN/NUMA/网卡探测
  decode-topology.json        D的对应快照
  cases.json                 精确输入IDs与采样参数
  reference/                 独立参考的响应和P/D日志
  round-01/ ... round-05/     outcome.json、P/D/router日志、启动环境
    events.jsonl             从日志提取的结构化事件
    events-*-rank*-pid*.jsonl 各进程的单独事件归档
```

D 的 `.../decode/` 保存本机完整日志，并接收相同的最终报告、样本、统计和output-analysis.json。
向开发侧回传 P 汇总目录及 D 目录中的 `summary.json` 和本机启动/清理异常日志。
保留旧 run-id，重跑使用新编号，入口拒绝覆盖既有运行目录。

离线重算只需 Python 标准库，不需要 NPU、模型或服务：

```bash
python3 ascend-mempool-test/scripts/mempool_perf_report.py \
  /home/cryang/log/mempool-min/baseline01-20261028-001/prefill
```

如仅有稳定性失败，先定位原始轮次和日志，再提供 JSON，例如：

```json
{"T_route/B": "填写已核实的具体原因、轮次和日志证据位置"}
```

```bash
python3 ascend-mempool-test/scripts/mempool_perf_report.py \
  /home/cryang/log/mempool-min/baseline01-20261028-001/prefill \
  --stability-explanations /ABS/PATH/stability-evidence.json
```

该文件只解释波动，不覆盖数据、正确性、缺事件或样本数检查。
原始运行失败状态保留在 `summary.json`；离线评估结果另在 `perf-report.json`，用户核对后记录验收。
新运行的perf-manifest.json显式记录core-answer-v1。旧归档未声明该策略时仍按旧严格规则重算，
不会把baseline01-001缺失的C/E或第5轮补记为已执行。

## 计时语义与限制

所有区间使用一个进程的 `monotonic_ns`；P各rank的绝对时刻互不相减。
跨机只有Main本机收发间隔。`backend_e2e_latency`原样使用D后端已有数，另标其时钟归属。

- Main的dispatch边界是第一条D dispatch实际发送。B/C恢复和fill在T_ready内。
- `response_sent`在Main的HTTP `write_eof`之后记录；不声称客户端已收到。
- P/D forward的完成由现有tick查询设备event观察。没有增加逐步synchronize；
  提交时刻也单独保存，查询延迟是已知测量上界，不把主机提交耗时当作设备完成。
- P写回完成在所有主KV/Index K producer写入后记录事件；与forward区间可以重叠。
- T_send从Ascend sender提交传输入口到原有handoff完成，包含传输排队。
- D的Index K ready来自原生receiver成功状态；handoff ready是含metadata gate与TP共识的成功观察。
  这些是服务可观察到达时刻，不是网络包物理到达时间；原生成功可能同时覆盖多项条件。
- T_first是首次真实scheduler输出发送。非流式可能首次一次发送50个token，原始事件记录当次数量。
  它不是客户端TTFT，也不能用批次输出间隔替代逐步T_step。
- T_op1/2与T_ready重叠，T_op3与T_tail重叠，不能重复相加。
  A/B残差是最后free_ack到Main完整响应发送，含后端响应尾部与Main处理/发送。

Main现有10ms轮询、SR两次TP gather及逐条ack往返只报告，不调优。
命令入队→实际发送按同一Main时钟记录，区间并集避免P/D同时等待被重复相加。
本轮历史11.12秒来自128输入/48输出；当前4096/5120输入、128输出不能数值分解那次旧请求。

阶段0生产启动检查拒绝显式 `rank_numa_map`，本票只跑当前配置一次。
当前8 NUMA连续均分已是每NUMA两个rank的私有副本，但不等于K1共享。
注册 `numa_node` 是请求分配，`numa_placement_verified=false`保留；不能声称物理页落点已验。
Ascend使用驱动/CANN/torch-npu/HCCL/MemFabric证据；CUDA/nvidia-smi/GPUDirect不适用。
未能取得的拓扑探测会明确保存错误，设备直传/RDMA未获证据时标未知。
