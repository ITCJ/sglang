# Ascend NPU 阶段 0 单请求对照基线

**Status:** accepted
**State:** closed

用户于 2026-10-28 授权先完成本基线，再看 K1。按用户提供的票 01 实施。
实现分支 `cryang/dev/mempool-min-baselinetest`，基点 `8869c2c605`。
本票只测量、报告，不调优、不改默认部署、不实现 K1。
真实 NPU 测试由用户运行；用户于2026-10-28确认 baseline01-002 验收，本票关闭。
用户补充：当前与 `cryang/dev/mempool` 分支目前 commit 的单请求性能基本一致，后续跟进优化。
人工验收证据见 [acceptance.json](verification/baseline01-002/acceptance.json)。

## 固定合同

- GLM-5.2，P/D 各一台 16 NPU，TP16、attention TP16、DP1。
- token page=128，S_P=8192、S_D=128，每份 prefix 40 页；私有副本 16/16/16/16、layer-first。
- 复用阶段 0 启动脚本，P eager、D Graph BS16；串行、单 chunk、非流式、deterministic 设置相同。
- 使用 ticket04 逻辑 ABCE 原始 token IDs：4096/5120/4096/4096，B=A+1024、C=A，E 首页不同。
- greedy、ignore_eos=true、max_new_tokens=128；无 stop/stop_regex/min_new_tokens。
- 每轮 A→B→C→E；命中 0/4096/3968/0，prefix 占用 0→32→40→40→40。
- 默认 5 轮，第一轮保留但不统计。整套服务每轮重启，prefix 归零。
- 每样本必须实际输出 128 IDs、length 结束，核心答案分别与独立标准答案核对，且命中与账本正确。
- 用户在 baseline01-001 后明确接受核心答案正确而强制续写不同：完整 IDs 差异仅作记录，不阻塞、不跳过 C/E，不再询问。
- 逻辑或其他检查失败仍标无效并保存计时，继续尝试 ABCE 与全部轮次；运行故障造成未执行则如实记录。
- 保存参考/实际完整输出、首差、逻辑检查、跨轮稳定性（含预热/无效样本），全跑完后统一分析，由用户决定是否通过。
- 每指标每适用用例至少 4 个有效非预热样本；自动检查失败仍非零退出。自动报告的user_accepted=false不推断人工验收，用户确认另存acceptance.json。
- V2 内容回读、V4 复制适配器、长生成、并发、故障/超时注入及 profiler 不进计时轮次。

## 指标

每区间只相减同主机同进程的 monotonic_ns；跨机仅取 Main 的本地收发。
Main：T_main_e2e、T_route、T_op2_load_back、T_op1_fill、T_ready、T_gen、T_op3_admit、T_tail、T_free_P/D。
P：每 rank 的 T_lb_rank、T_prefill、T_wt_rank、T_send、T_hbm_release；汇总取 16 rank 的 min/max。
D rank0：T_gate、prompt/Index K/handoff 各到达时刻、T_first、每一步 T_step、T_finish、后端 e2e_latency。
T_step 预期 127 步，保存实际每步，第一步单列；n/p50/min/max，并另报 p90/p99。
其余指标按指标×用例给 n/p50/min/max。原始样本有 round/case/rid/metric/value_ms/clock/valid。

Main dispatch 边界是第一条 D dispatch 实际发送；B/C 的恢复和 fill 计入 T_ready。
T_first 是 D scheduler 首次实际向输出通道发送，保留原有非流式刷新粒度，记录当时 token 数。
设备完成时刻由现有 tick 非阻塞查询观察，报告明确其轮询上界；不将提交时刻当完成。

## 交付与验收

每机一条 perf 命令，自动启动、等就绪、原生预热、重复、请求、验证、归档、清理。
离线报告器从归档重算并产出 Markdown、JSON 统计和 JSONL 原始样本。
报告含版本、配置、拓扑、rank→NUMA 注册、Ascend 驱动/CANN/torch-npu/MemFabric/拷贝源码身份。
P↔D 网卡、RDMA/设备直传证据按可观察字段记录，未验证项不能猜测为已启用。
A/B 每轮拆分 e2e=route+ready+gen+tail+残差，单独解释 residual 与消息往返。
固定等待定位到源码，估算占比；max/min>1.5 时加轮或解释，未解释则报告不完整。
混淆项包括非流式、Graph padding、串行、单 chunk、NUMA，以及逐步事件查询/日志成本。
baseline01-002 的13项局部计时波动已量化记录，用户接受当前基线并将进一步定位与优化留待后续；原始自动failed状态保留。

## NUMA 前置结论

阶段 0 不支持显式 rank_numa_map 启动：`sharing_plan.require_rank_pair_startup` 拒绝它。
当前 numa_nodes=[0..7] 已将 16 rank 连续均分，每节点 2 个私有副本。
只跑现有映射的 5 轮；不为第二映射改生产路径。
注册的 numa_node 是请求分配，numa_placement_verified=false，不能声称物理页已验。
K1 将改变共享和 NUMA 使用，仍属对照混淆项。
