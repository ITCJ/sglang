# Ticket14 独立带宽刻画

不启动 SGLang、不加载模型。复用生产 BM 会话/映射、page-first KVLayout、PageWriter、PrefixLayout 和部署的64位UniDexCopy。
这是微测工具；CPU检查不能证明NPU正确性或性能。每份报告保留 `user_accepted=false`，收到用户确认后才关闭14。

## 环境与存储

在31/P与32/D现有CANN环境运行。使用18验收的64位UniDexCopy和支持batch错误传播的MemFabric SDK；入口自动记录两侧版本与包内`.so` SHA256。
Python须有 torch/torch_npu、sgl_kernel_npu、memfabric_hybrid、msgspec、matplotlib。Matplotlib只生成离线SVG，不采集设备trace。
两机源码必须一致。需要可用设备0–15、NUMA 0–7；数据端口从21064起，控制21000、协调21001、BM store21002；完整对比额外使用21003/21004。
按真实网络、设备和NUMA拓扑调整参数，两机使用相同逻辑rank顺序。端口范围须留出SDK按rank分配的NIC端口。

默认页大小64，78层，1152 B/token/layer；Index K为21个producer层、256 B/token。
默认每机8个NUMA、每组两rank。P申请193 GiB，D申请176 GiB；这是微测容量，不是模型/服务预算。
容量覆盖128K两块互不重叠的读写区域，并保留生产映射结构。各rank的物理贡献与页列表在报告中。
`--describe`只输出计划与分配量，不接触NPU。`--suite full`要求16设备与8节点，防止小拓扑冒充全矩阵。

## 第一组：远端写、双写、D读干扰、连续逐层写

在仓库根目录，31/P执行：

```bash
bash ascend-mempool-test/scripts/run_bandwidth.sh \
  --role prefill --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket14-001 --suite remote-write --page-size 64
```

32/D执行：

```bash
bash ascend-mempool-test/scripts/run_bandwidth.sh \
  --role decode --prefill-host 10.120.72.31 --decode-host 10.120.72.32 \
  --run-id k2-ticket14-001 --suite remote-write --page-size 64
```

P可以先启动等待D；两侧自动握手、建池、确认映射就绪、测量、设备内容检查、报告互收及自身进程清理。
不需要模型路径或额外服务。运行期间会输出 `BEGIN`/`PASS` 档位；父进程默认最长等待1800秒，可用 `--timeout` 配置。
结束必须两侧退出码0并输出 `TICKET14_MEASUREMENTS_COMPLETE`。失败退出非零，保留原日志，重跑用新的run-id。

每档3次，运行内先预热3次、测100次操作；不为每档重建池。`--iterations`和`--warmup`可配置，报告保留实际n。
已有有效同机层计算窗口时，可在两机追加 `--layer-window-ms 数值`。它仅用于初步比较，不能证明模型中的真实重叠。
没有计算窗口时，隐藏结论明确为unknown；不自动开启trace或要求重新跑13。

## 完整矩阵与smoke

完整矩阵用同一对命令将 `--suite remote-write` 改成 `--suite full`，使用新run-id，例如 `k2-ticket14-full-001`。
它包含远端写组；若先跑远端写再跑full，同一档位共两轮，不安排第三轮。

完整范围：

- P→D：4K/8K/32K/128K的逐层新算KV，只写P、只写D、双写、只写D+D读、双写+D读。
- D同读者数、同数据量的无写读基线；背景读逐次完成延迟、p99及窗口内操作数。
- 78层连续入队，保留源缓冲直到库完成；记录每层排队/拷贝时间、源内存占用、末层排空。
- 两个方向的1/2/4/8/16读者压同段，以及16读者分到8段、每段两读者。
- token随机行、页-层块、整页；固定每读者逻辑目标量，块粒度向上取整的实际字节单列。
- 本机参考段、其他NUMA、远端机器；独立读/写结果；同NUMA叠加128K逐层顺序写。
- 约1/4/16/147KB、每步2048个小读；自身路径与可部署时的Mooncake get。
- 物理NUMA驻留抽样；driver映射不暴露页计数时保留unknown，不拿配置代替物理证据。

`--suite smoke --devices 0 1 --numa-nodes 0 --iterations 2`提供小拓扑检查；仍有真实NPU内容验证，但不是完整验收。
不要把smoke报告或`remote-write`报告当成全矩阵通过。

## 测量口径

4K/8K/32K/128K表示本层新算token数。25单chunk时等于未命中的prompt段；20多chunk后8K是主要参考档。
命中数据不计入双写。每页-层块=page_size×1152 B；整页再乘78，页64和128分别生成对应字节。

远端写统一使用BM L2G batch；提交和wait在同一线程。每个batch最多256段，计时包含所有批次提交与完成。
默认16个P写者按完整页分工，共同写D第0组的一份alive；P prefix候选页按现有算法在8段间交错。
D alive使用散页地址（模容量的17倍置换），不会用连续大块峰值替代page-first写。P本机与D远端均用L2G，Index K只写P。
测量记录逻辑主KV字节、包含Index K的实际流量与每段带宽。不得把这份单副本流量乘TP16。
这是一份明确的25候选写分工；25若改写者数或执行路径，应重用本程序对应配置复核，不能直接搬用旧数。

读取用部署的UniDexCopy：token行一行一次索引，页块在合法kernel行宽内分块；所有输出落NPU HBM。
计时是eager主机提交到真实完成，包含Python/SDK开销。不是Graph-only或纯DMA峰值。
多读者总带宽按同机最早开始到最晚结束的跨度计算；报告并发重叠比例，无公共区间不能标为通过。
“本/其他NUMA”相对于rank0；多读者档横跨配置的各rank拓扑，不声称16个设备都在同一NUMA。
交错档保持每读者工作量不变，各组读取各自段；不包含all_gather，也不证明完整请求重建延迟或容量收益。
读写区域互不重叠，背景写不会造成读数据竞态。背景读窗口含控制边界等待，报告明确保留这一测量限制。

每档3次取带宽中位数；p99来自运行内部完成时间样本。小读自身p99是2048-slice整步，不是单slice延迟。
连续逐层测试是**无计算间隔的传输压力测试**。队列与末层排空可观测，但真实计算竞争和隐藏效果留25集成验收。
“达到理想线性80%”只作为结论中描述曲线的约定，不是性能验收门槛；原始点全部保留。

## Mooncake

full自动检查两侧 `mooncake.store` 与P侧 `mooncake_master`，可用时启动本次独占master和客户端。
P贡献512MiB对象段，D只作为读取客户端；无需外部常驻服务。默认`--mooncake-protocol rdma`，可按部署指定`--mooncake-device`。
缺包、缺master或setup不支持时，记录实际异常和日志，交付自身路径；不会把缺测写成性能结论。
部署成功后的内容错误或测量失败仍使整次运行失败。可显式选择tcp，但结果会标注协议，不能当RDMA结果。
Mooncake get终点是CPU bytes，自身路径终点是NPU HBM；报告同时给ops/s、CPU核占用、单get/整步p99，明确终点差异。

API依据：[Mooncake Python API](https://github.com/kvcache-ai/mooncake/blob/main/docs/source/api-reference/python/mooncake-store.md)、[部署指南](https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/deployment/mooncake-store-deployment-guide.md)。具体可用性以部署版本的真实setup结果为准。

## 回传与关闭

两侧目录：`/home/cryang/log/k2/<run-id>/prefill|decode/`。
每侧均收集peer的rank报告与日志：

- `report.json`、`summary.csv`、`conclusions.md`和各测量组SVG：范围、实际样本数、曲线、U6/Q24/Q27/Q7/Q5结论。
- `rank-*/samples.json`：逐次完成延迟及字节；`pipeline.json`：逐层队列与排空；`background.json`：干扰流。
- `workloads.json`：页列表、源偏移、写目标、实际kernel分块；`mapping.json`、`residency.json`、`environment.json`：映射、落点、版本证据。
- `identity.json`、`runner.json`、`rank-*.log`：源码身份、父进程清理、执行日志。失败另有`failure.json`。
- full中的`mooncake/`保留原始操作样本，`mooncake.json`保存简表或缺测原因。

先回传两侧report、conclusions与runner。错误时回传首个失败rank日志。CPU和NPU状态分开记录；用户确认后才更新ticket。

## 001：同源码独立构建 SDK 的离线恢复

用户确认31/32 SDK源码HEAD相同、分别编译；各机16个rank内部SHA一致，跨机SHA不同。
允许通过 `--sdk-source-confirmation` 记录人工源码确认，不伪造尚未提供的源码SHA。
原始二进制SHA全部保留；同机不一致、缺证据、kernel/torch差异仍拒绝。
正常运行入口也支持该参数。确认SDK源码一致不等于用户已确认NPU验收。

001的runner为measured/cleanup passed，原始采样可直接离线处理，不启动NPU或双机连接。
在31执行（已包含peer证据，只需这一台）：

```bash
python3 ascend-mempool-test/scripts/analyze_bandwidth.py \
  --input /home/cryang/log/k2/k2-ticket14-001/prefill \
  --output /home/cryang/log/k2/k2-ticket14-001-analysis/prefill \
  --sdk-source-confirmation '用户已核对31和32 SDK源码HEAD一致，两侧分别编译'
```

若也需要32独立生成报告，将input/output最后的prefill改成decode。
输出目录必须不存在；原001失败报告、原始采样及环境文件均不修改。
返回0且输出 `TICKET14_ANALYSIS_COMPLETE` 后回传新目录report.json与conclusions.md。
其他覆盖、内容状态、并发、统计检查继续执行；后续若另有失败，保留输出并回传错误。
