# K1-ticket03：prefix 分工读取验收

本票在16份prefix布局上实现`split_read`。
采用2026-10-30合并缓冲定稿：直接H2D → in-place all_gather → 单核索引散开。
两块汇总缓冲层间复用，一张槽位表供主KV与Index K共用。
主KV仍拆回512维和64维缓冲，全部rank完成恢复后才返回`load_back_ack`。

## 运行

沿用ticket02已验收的31/32环境、权重、网络接口和配置。
P/D各TP16，page128，P slot8192、D slot128，P eager、D Graph。
环境需要Triton-Ascend（原生NPU内核已使用该依赖）；新增散开核首次恢复时JIT编译。
两机使用同一代码版本，在仓库根目录执行。先P后D；入口自动等待并协调后续步骤。

002在模型初始化期间因`BadStatusLine`提前退出。测试入口已修复该异常的有界重试；
只有HTTP 200才算就绪，超时显示目标URL及最后异常，进程失败仍立即中断。
回显来源尚未证实。003已完成A，但B恢复在私有prefix的CPU比较处SIGSEGV。
004的host映射修复触发HalMemSetAccess 65534，BM创建失败，默认清理后返回137。
现恢复原映射，V2通过NPU读取实际私有副本，复用scatter之后的汇总缓冲。
005用于复测；003/004均未通过。详见[004故障记录](../.scratch/k1/verification/k1-ticket03-004-20261030/README.md)。

31（P）：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode split-read-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket03-005
```

32（D）：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode split-read-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket03-005
```

新环境的host、model_path和interface配置方式见[K1-ticket02](K1_TICKET02.md)。
两机run-id必须相同且未使用；`--describe`可预览，不加载模型。
入口自动完成准备、就绪检查、请求、结果核对、报告收集及自身进程清理。
失败返回非零并保留日志，不自动重跑。

## 两轮覆盖

1. `target`使用`round_robin`：逻辑页i交给rank `i % 16`。
2. `restart`使用`contiguous`：每rank获得一段连续页，多出的页依次分给前面的rank。

每轮都运行相同的A/B/C/E、128-token负载和提前EOS。
A/C/E输入4096 token，B输入5120；B命中4096，C命中3968。
因此B每rank读2页，C最后一个rank只读1页，其通信缓冲补齐到2页。
补齐页只参与通信，不读取prefix、不写入原生页表。

首轮请求完成后验证超时fail-stop，第二轮验证同端口重启。
退出沿用SGLang父进程清理；检查双端进程与端口，不要求逐rank主动释放回执。
P-only/D-only/mixed复制smoke仅运行一次；不增加第三次服务启动。

## 通过条件与范围

- 原有KV内容、Graph、来源读取、prefix保存/命中、非连续HBM页、后缀计算、
  延迟fill、ready/free账本、槽位复用、故障与双端清理检查通过。
- 每个rank逐层核对all_gather汇总缓冲、散开后HBM与私有prefix，主KV和Index K均精确相等。
- 核对生产恢复每类每层H2D/all_gather/散开各一次，主KV78层、Index K21层。
  V2另有每层两次诊断NPU读取及CPU取回，复用同一汇总缓冲，分别核对gather和原生内容。
- 两块KV缓冲分别为4,718,592B与1,048,576B；首次恢复每rank一行`split_read_buffers`日志。
- 槽位表只建一次，含有效token行；31页命中跳过rank15第二槽位，两个策略均通过V2。
- `read_bytes`只统计分工数据读取；全rank总和等于命中数据字节，每有效页恰好一次。
  `oracle_read_bytes`另记验收核对产生的全副本读取，不能当作生产路径流量。
- SR在每个rank设备event完成后收集回执。
  P机统一单调时钟上的`ack_ns`必须晚于全部`completed_ns`。
- 两种策略使用同一组input IDs；答案均符合独立fixture。
  报告保存完整output IDs差异，沿用[逻辑答案口径](../.scratch/mempool-min/output-verification-policy.md)。

本模式是K1-03功能验收，复用K1-02两轮精简范围。
V3用独立标准答案，不另启正式参考服务；V8用实际注册在CPU重放容量拒绝。
不重做冷A/C稳定性，不验证新的物理NUMA落点，不重跑旧五轮性能基线。
本次用户授权把四段诊断计时加入当前commit；baselinetest分支暂不修改。
这些范围不等同于原`full-acceptance`全部覆盖；复制smoke单独也不构成本票验收。

## 报告

两机日志根目录：`/home/cryang/log/mempool-min/k1-ticket03-005/`。
请回传P侧`prefill/summary.json`、`prefill/split-read-acceptance.json`、
`prefill/split-read-timings.json`及`.md`和两机终端结果。
失败时同时保留对应阶段的`prefill.log`、`decode.log`和`snapshots.json`。

`target/`和`restart/`各存实际`effective-config.json`、注册、输入输出、答案、
KV及生命周期证据、容量重放、进程和端口清理结果。
根目录保留版本、代码差异、模型/依赖身份和拓扑快照。
`split-read-acceptance.json`包含两策略读取量、内容与完成证明及输出比较。
脚本保留`user_accepted: false`；用户确认后才在ticket记录验收并关闭。

普通服务配置可选`"prefix_read_assignment": "contiguous"`，默认`round_robin`。
验收模式固定依次运行两种策略，逐轮保存生效配置。

## 四段计时与历史对照

验收模式自动开启临时`profile_prefix`，普通服务默认关闭。
H2D、all_gather、scatter用恢复stream上的NPU event计时并跨层累加。
T_lb_rank从本rank提交恢复到观察到完成event；包含调度观察延迟，不含之后的HBM内容核对。
op2是Main下发至收到完整ack的同进程墙钟区间。

每次请求选择设备三段合计最大的rank，三段均取该rank数据。
控制余量=op2减去这三段，包含建表、排队、CPU验证、首次JIT和回执等待。
不把不同rank的三个最大值相加，也不把NPU时钟与CPU时钟绝对值相减。

每策略每用例只有1次请求，表中明确n；T_lb_rank保留16个rank的原值。
历史baseline01-002的op2及rank min/max行并列输出，旧基线的四段记为未测。
V2的额外NPU读取和CPU取回会影响T_lb_rank、墙钟及rank间等待，
不能据此宣称生产吞吐收益或纯通信时间；生产三段event不包围这些诊断读取。
新表不设性能门槛；回传后按分段分析变差，必要时再安排关闭V2的对照。

用户确认验收后清理临时工程测试接线，具体清单见[交付说明](../.scratch/k1/delivery-ticket03.md)。
