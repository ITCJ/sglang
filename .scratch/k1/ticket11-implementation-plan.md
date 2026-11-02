# Ticket 11：工程接入草案与逐 op 路径

日期：2026-11-02。状态：**仅规划，待 demo 结果和用户路径确认后定稿**。
基线 `abdee9d701`；12 已验收关闭，11 open，09 仍阻塞。

先执行 [demo 交接](ticket11-demo-handoff.md)。本文不能作为跳过 demo 或用户核对的生产实施授权。
用户明确要求规划 agent 不写实现；high 在后续获准阶段实施。
规格依据：[ticket 11](issues/11-k1-02c-pool-copy-executor.md)、[执行文本第15节](k1-tickets.md)。
SDK依据按用户要求固定为本地 `../gitcode/memfabric_hybrid`：GitCode origin，1.3.0，`29fbd1ae14e7`；不使用较旧GitHub版本作实现判断。

## 当前阶段补注

用户已确认路径并授权demo实施。共用pool_copy平台适配层与两组P/D demo已交付。
后续工程只复核接口事实，不再次选择op路径；取消八组扩容demo。
服务验收模式也不新增目标读回，沿用现有V2及B/C证据；直接目标内容证据由demo提供。
下方旧“demo后定稿路径/服务验收目标读回”建议已被此决定取代。
生产PrefixStore/SR尚未接入；NPU未运行；当前入口见ascend-mempool-test/K1_TICKET11_DEMO.md。

## 1 编码与协作规矩

| 约束 | 本票如何落实 |
|---|---|
| 尽快交付简单可运行 demo | 先做无模型、少页的真实 BM/SDMA 验证；复用既有初始化和页几何 |
| 复用已有实现与历史 | 保留 07/08 的选页、分片、覆盖位图，使用 12 的 `page_block`；沿用 source 保护及完成汇总 |
| 少加防御逻辑 | 不引入重试、回退和通用任务框架；必要的新保护先说明并获同意。既定的错误上报/fail-stop 继续执行 |
| Ascend/NPU 隔离 | 预计功能源码只改 `hardware_backend/npu/mempool/` 与 `disaggregation/ascend/`；通用主线净改动目标为 0 |
| 主线修改先说明 | 若实际必须改通用源码，先列文件、原因、行为影响和 hook 不足，再实施 |
| 默认退出 | 沿用父进程清理 worker；不要求逐 rank 主动释放回执，不增加通用 shutdown 协议 |
| 精简验证 | 关键接口正确性和两轮服务验证；同一测试最多 2 轮，不重跑 08/12 模型作性能基线 |
| 关闭标准 | CPU、代码完成与用户 NPU 验收分别记录；用户确认后才关闭11、解锁09 |
| 交付 | 工程尽量一个 ticket commit；Standards/Spec 两轴评审；功能源码按专用/通用分列，测试和说明另列 |
| 同步进度 | 实施每步更新目标页 D 并重生成 HTML；同步 ticket Comments 和 K1 memory |

## 2 已核实的现状与接入点

- [PrefixStore.admit](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py:264) 提交一次，再查询设备 event。
- [PrefixStore._read](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py:368) 共用 fill/load_back 的事件轮询；二者目前进入 `_read_context` 的独立设备流。
- [_submit_fill](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py:743) 按已有 `PageWriter.pages(完整 prompt 长度)` 选择本分片页。
- [_submit](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py:851) 按目标 prefix 段分派 admit，默认各段 owner 写本段。
- [_copy_region](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py:819) 目前逐层调用 `copy_rows/UniDexCopy`。这是本票替换的主要搬运路径。
- [page_block](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py:744) 已返回 `(SDK GVA, device address, page_bytes)`。
- [RouterShard](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py:338) 已控制 restore/fill/admit 的前置条件。
- [MempoolSubrouter.advance](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py:563) 已在 tick 内执行、全 rank 汇总、完成 admit 和向 Main ack。
- [runtime 构造](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py:1108) 已有设备、manager、PageWriter 与 PrefixStore 的装配点。

当前是同一 P/D 32 参与者 BM 会话里的多个逻辑区域；“池间”指 alive/prefix 区域之间。
不创建第二套 BM handle，也不承诺任意独立 handle 之间可以 G2G。
数据池所在的 host/NUMA、物理 owner、执行分片、执行 NPU 是四个独立概念。

## 3 逐 op 传输路径（待 demo 证实）

令组 `g={2g, 2g+1}`，默认 g=0…7；`owner(g)=2g`。
`NPU(P,r)` 指 P 侧 rank r 的 BM 初始化设备，实际取 `model_runner.gpu_id`，不硬编码为 r。
表中 NUMA g 是当前默认映射的简写；自定义映射以 `PoolSession` 为准。
P/D 的 rank 0 各是自己的控制协调者，不是全局数据搬运中心。

| op / 数据 | 源物理区域 | 目标物理区域 | 执行分片 | SDMA 所属设备 | 完成消费者 |
|---|---|---|---|---|---|
| op3 admit 主 KV | P NUMA g 的 alive main 副本 g | P NUMA g 的 prefix main 段 g | P owner(g) | NPU(P,2g) | 所有有工作 owner 完成，再全 rank 汇总、admit_ack、Main publish |
| op3 admit Index K | P NUMA 0 的单份 alive Index K | P NUMA g 的 prefix Index K 段 g | P owner(g) | NPU(P,2g)；g>0 为跨 NUMA 读取 | 与该分片 main 同一张 batch，一起完成 |
| op1 fill 主 KV | P prefix main 的所有命中段 | P NUMA g 的 alive main 副本 g | P rank 2g 和 2g+1，各搬自己负责的页 | 各自 NPU(P,r) | 分片 filled/覆盖位图，再全体 prompt ready 汇总 |
| op1 fill Index K | P prefix Index K 的所有命中段 | P NUMA 0 的单份 alive Index K | 默认 P rank 0 | NPU(P,0) | 与 rank0 main 同一张 batch，进入 fill 完成汇总 |

fill 的目标是 **P alive**，不是 D alive；D 后续读取 prompt 的路径沿用现状。
执行设备由调用分片选择，SDK 内部选择实际 stream/SDMA 队列；不能预先指定或声称某条物理 SDMA channel ID。
表中的候选设备来源有源码依据，实机版本和 peer 可见性仍须 demo 验证。

**具体例子：** prefix 页位于段3、段内页1（当前编码为16）。
admit 时 P rank6 用自己的 NPU，把组3 alive 主KV页与 NUMA0 alive Index K 页分别搬入该段两个区域。
之后 fill 到组5时，由 rank10/11 中负责该请求逻辑页的分片用自己的 NPU，将段3主KV页拉到组5 alive。
同页 Index K 由 rank0 用 NPU(P,0) 拉到 NUMA0 alive Index K。
页归属按完整请求长度决定，不能因为这次只命中31/32页而重新平分给两个分片。

以下 op 仅作为边界说明，本票不替换它们：

| op | 现有数据路径 | 本票处理 |
|---|---|---|
| op2 load_back | P prefix DRAM → 分工 reader 的 HBM 汇总缓冲 → all_gather → 原生 HBM 散开 | 继续使用现有设备 copy/事件与 collective |
| P write_through | P 模型 HBM → P alive main / Index K | 保留 PageWriter 和源完成事件 |
| D 每步写回 | D 模型 HBM → D alive main | 保留特征分写、图与每步完成顺序 |
| P→D Index K handoff | 既有原生传输路径 | 保留，与本票池间 copy 分开 |

`single_rank` admit 配置继续可用：选定分片读其自身组的 main 副本，写所有目标段；Index K 仍取单份源。
因此只有默认 owners 模式的主 KV admit 写本 NUMA；不要给 single_rank 也标注“全本地”。
现有多 Index K writer 配置也保留其 PageWriter 分工；表中 rank0 是当前默认。

## 4 清单与地址计算

保留 `PrefixLayout.tasks/decode`、`_admit_writer` 和 PageWriter。
每个执行分片在有序 tick 中一次生成不可变清单；工作线程只提交已经解析好的字节块。
描述符、source/target 页配对和完整请求长度保持现有校验。

| 操作 | 源页 | 目标页 |
|---|---|---|
| admit | alive physical page = `slot × (S_P/B) + source_pages[position]` | 将 `target_pages[position]` 解码为 `(segment, local_page)` |
| fill | 将 `source_pages[position]` 解码为 `(segment, local_page)` | alive physical page = `slot × (S_P/B) + position` |

用 `manager.view(0, 0, region=..., segment=...).page_block(physical_page)` 取块。
SDK 清单仅取返回值中的 **GVA 和 bytes**；device address 留给既有设备内核/验证路径。
main 与 Index K 各用自己的 geometry；不要用主KV stride 推导 Index K 地址。

每分片每 op 只调用一次非空 batch；main 与 Index K 放在同一批。
第一版交给 SDK 做相邻块合并，应用层不另写合并优化器。
可按区域/段组织清单；统计原始页块数及源目标都连续的可合并 runs。
SDK 没有实际任务数观测时，后者命名为 `coalescible_runs`，不能冒充实测 SDMA 任务数。
每个块/连续 run 的字节数也要符合已核对 SDK 的长度限制；当前几何下各分片远小于4 GiB。

| N 页 | 原始块数（所有分片求和） | 有效字节 |
|---|---:|---:|
| admit | 2N | N × (M+I) |
| fill，8份 main、1份 Index K | 9N | N × (8M+I) |
| admit，N=32 | 64 | 390,070,272 |
| fill，N=32 | 288 | 2,966,421,504 |

不把无工作分片、通信 padding、验证读回或重复轮询计入搬运字节。
零工作分片返回空完成结果，仍参与现有16-rank collective 次序；8个 admit writer 不等于只让8个 rank 调 collective。

## 5 最小接口与线程所有权

建议新增一个 NPU 专用 `pool_copy.py`，集中任务数据、队列、执行线程和结果。
接口名是候选，不要求机械照抄；职责与完成条件是合同。

| 接口/数据 | 合同 |
|---|---|
| `CopyBlock` | src_gva、dst_gva、nbytes；日志关联 region/page/owner，避免在线程里解析模型对象 |
| `CopyTask` | 请求标识、op_id、kind、不可变 blocks；manager/会话生命周期覆盖整个任务 |
| `submit(task)` | 仅入队，立即返回；每个 op 由 PrefixStore 的现有记录确保只提交一次 |
| `poll(op_id)` | 非阻塞取本地完成或错误；没有结果时返回 pending；不会调用 BM wait |
| 执行线程 | 绑定 NUMA/设备 → 一次 batch → 同线程 wait → 发布结果；不访问 torch collective、Main、allocator或PrefixStore可变状态 |

manager 增加一个很小的 BM batch/wait 适配方法，集中 `BmCopyType.G2G`、flags 和返回码处理。
可在这个方法内部先 batch 再 wait，但只允许执行器线程调用。
对 PrefixStore 暴露异步 submit/poll；不要向 scheduler 暴露 handle 或要求它理解 SDK stream。
通过注入真实/fake adapter 测试接口，不新增多后端插件框架。

每个 P worker/SR 分片一个长期线程；D 不执行 op1/op3，第一版不为空路径创建线程。
线程只绑本线程，不改变 scheduler 的 CPU affinity。NUMA集合与容器允许集合取交集。
设备沿用该 worker 的 BM 初始化设备；线程首次调用时设置设备上下文。
本地 SDK `BatchCopyData` 调用 `SetThreadAclDevice`；其状态为 thread_local。
`Wait` 本身不重新设置设备，须在已完成 submit 的同一线程调用；实机仍需验证。

固定一线程最符合当前串行设计。规格提到“线程数配置、默认1”，但多线程池的排序/等待语义尚未定义。
**建议首版仅1线程，保留绑核开关和测试延迟配置；若用户要求线程数可调，先补设计，不由high扩写。**
这是待核对的规格细化，不是本轮已批准变更。

## 6 PrefixStore 和 SR 如何衔接

1. 保留已有源写完成、native 清理、prefix 源保护与目标预留前置条件。
2. `_submit` 和 `_submit_fill` 生成页块任务，进入同一执行器。
3. `_Admit` 与 fill 的 `_Read` 轮询执行器结果。load_back 继续查询设备 event。
4. 将 fill 从 `_read_context()` 的包裹中移出；该设备流仍服务 load_back。
5. 成功后由 tick 主线程构造现有 receipt、统计与验证结果，再更新本地状态。
6. `complete_admit` 沿用全 rank 收据、页单 writer 检查及本地 `_ready` 更新。
7. `MempoolSubrouter.advance` 沿用两次有序 gather 和 rank0 对 Main 的 ack。

fill 以真实 copy 完成推进 `fill_complete` 和 `PromptCoverage`，`fill_ack` 保留。
admit 收齐 writer 可读结果后，才能 `admit_ack → Main publish → free → free_ack → 额度归还`。
重复 tick 只轮询，不再次 submit；零工作结果也遵循已有 op 身份和汇总规则。

线程异常/非零 SDK 返回码通过结果队列交还已有 fault/fail-stop 路径。
任何失败或未知完成状态都不产生 readable receipt，不释放源/目标、不归还额度。
不能以 SDK wait 异常后“看起来目标有数据”继续业务，也不能自动重试半完成 batch。

## 7 完成桥接与退出

执行器使用内部 BM stream 的候选路径：`batch(flags=1,stream=0) → 同线程 wait()`。
它与 torch 的模型流、load_back 流不同。源 ready 由既有写回事件完成与全 rank 事实证明。
wait 成功是库完成信号；demo 中用目标读回和 peer reader 验证实际可见性。
生产消费端仅在该信号经 tick 汇总后继续，不添加全设备同步。

当前 Main 的单请求串行门保持不变；本票证明搬运不会阻塞控制线程，不宣称已实现跨请求流水或吞吐提升。
记录 queue→submit、submit调用耗时、wait/完成、ack；比较因果关系，不把多机未经校准的 monotonic 时间直接排序。
Main 的 publish/free 使用现有事件记录与 op_id 串联；优先不改 MainRouter 功能代码。

正常服务请求释放只检查任务确实完成。服务退出继续由原父进程结束 worker。
执行器线程可随进程生命周期存在；不在调度 tick 或解释器 atexit 中增加无期限 join。
独立原型/测试在全部任务完成后可显式停止线程，再走原有 pool 关闭流程。
故障下库 wait 若不返回，Python 无法安全取消原生 SDMA；沿用已有超时/父进程 fail-stop。
验收检查本服务进程、线程随所属进程终止、实际端口可重绑。
**这不证明显式 BM 析构无异常**；既有 memfabric 析构问题如再出现，独立记录，不改通用 shutdown 掩盖。

## 8 候选文件净改动清单

按 high 的阅读/实施顺序列出；实际无需修改的文件从交付清单删除。

| Ascend/NPU 功能源码 | 用途 |
|---|---|
| `hardware_backend/npu/mempool/manager.py` | BMHandle batch/wait 类型与最小调用适配；沿用 page_block |
| `hardware_backend/npu/mempool/pool_copy.py`（新） | 一线程队列、任务结果、NUMA/设备绑定及库完成 |
| `hardware_backend/npu/mempool/prefix.py` | 两种清单构造；admit/fill改为submit/poll；load_back保持event |
| `hardware_backend/npu/mempool/runtime.py` | 用现有manager/device/NUMA装配执行器 |
| `disaggregation/ascend/mempool_router_protocol.py` | 若采用配置，增加最小的绑核/测试配置并进入已有配置记录 |
| `disaggregation/ascend/mempool_subrouter.py` | 仅在现有收据/fault接口不足时适配或加必要时间日志；不改消息合同 |

通用主线功能源码：预计 **0**。`scheduler.py`、`server_args.py` 不在计划中。
`sharing_plan.py`、`layout.py`、`page_write.py` 原样复用；SDK若需修复单列范围和版本，另行确认。

测试/脚本/说明另列：现有 `test_prefix_shared.py`、PrefixStore/SR关键回归，新的执行器接口测试、
`verify_pool_sdma.py` 原型引用、服务证据检查器和 `run_mempool_min.py` 的11自运行入口、`K1_TICKET11.md`。
沿用已修复的实际端点与 TIME_WAIT 检查，不复制另一套。

## 9 验证方案与尚待用户核对的证据范围

**CPU 最小集合：**

- 独立 oracle 核对31/32页、跨槽/段和混合M/I地址；2N/9N块与字节守恒，零工作rank，single_rank配置。
- pending→success/error、重复poll不重复提交；SDK submit/wait失败不产生可读收据。
- 延迟完成时仍可推进控制；最后一个有工作rank完成前不ack/publish/free，空rank保持collective顺序。
- 保留 load_back 事件路径关键回归；不新增逐文件镜像测试。

**NPU：**先独立demo；再复用两轮六例服务启动，记录实际覆盖与n。
服务中验副本/签名、页分工与字节、真实内容、ack顺序、负测试、清理/重启；不重跑08/12作性能表。
调度间隔记录空闲/搬运分布与max，验证搬运持续期间tick和heartbeat能推进；不要求有噪声的实机数值逐项相等。
同时记录原始batch与wait的耗时，不能用控制线程sleep注入冒充库本身释放GIL的证据。

**内容证据有一处实质差异：** ticket 11 V2 要求 op1后alive、op3后prefix 与源逐页一致。
当前 `_verify_shared_read(fill)` 只检查已验证的load_back源快照，不读fill目标；不能直接沿用并宣称新增目标校验通过。

建议分两层：demo对合成目标做完整独立读回；服务在验收模式下、库完成后，对实际目标按页读回小缓冲，与原计算源摘要比较。
main的8份fill目标与单份Index K分别覆盖；admit由各段实际目标读回。校验与纯搬运时间、调用计数分开。
这属于测试路径，正常服务不加主存读回，也不改地址所有权。
服务读回的开关/时点与额外时间须在正式实施前获用户核对；如用户继续接受12的较窄边界，明确将该V2项记为未覆盖/按授权缩减，不能勾成完整通过。

SDK子提交错误传播是独立门槛。源码已见的缺口不能用fake adapter负测或非法参数负测替代。
若实机相同SDK仍有缺口，先评估最小SDK补丁/已修复版本，并向用户说明；本方案不授权替换外部库。

## 10 分阶段实施交接

| 阶段 | 执行者 | 完成条件 |
|---|---|---|
| P0 方案核对 | 用户 + 规划者 | 确认demo优先、逐op候选路径和证据范围；本轮不写代码 |
| P1 独立demo | high；NPU由用户运行 | 按demo交接提交代码和实机结果；停在结果交接 |
| P2 路径定稿 | 规划者 + 用户 | 依据实机记录确认执行NPU、SDK版本/完成错误契约、验收内容；用户确认后授权工程 |
| I1 清单与执行器 | high | 关键CPU接口验证，页面D记录“代码已实现/NPU未验收” |
| I2 PrefixStore/SR接入 | high | admit/fill迁移，load_back回归与时序测试通过，主线改动必要性复核 |
| I3 集中交付 | high | 精简CPU/静态、Standards/Spec、代码清单、自运行入口；尽量一个工程commit |
| I4 NPU验收关闭 | 用户 + 实施者 | 分析用户结果并确认关闭11，才解锁09 |

P2 前的所有接口、文件和测试安排都是供核对的工程草案；demo可能改变具体SDK调用，但不自动改变已确认的页归属与控制语义。
本轮不更新正式规格的验收要求、不改ticket状态、不提交代码。
