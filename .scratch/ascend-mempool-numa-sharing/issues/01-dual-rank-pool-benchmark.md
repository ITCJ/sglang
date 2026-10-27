# 01: 独立池／共享池双 rank 读写对照

**What to build:** 在相同两张 NPU 和 NUMA 上，对比两个独立 mempool 各自读写，
与一个共享 mempool 由两个 ranks 同时分工写入、各自完整读取的性能。交付真实
BM 映射、物理去重、内容校验和可重复的 eager/Graph 测量，让服务接入建立在
可用的读写带宽与 SDK 能力上。

**Parent:** [MLA NUMA 共享 spec](../spec.md)

**Blocked by:** None (can start immediately)。现有 mempool 基础已验收。

**Status:** ready-for-agent

**State:** closed

**Accepted:** 2026-10-14，用户明确“ticket01可以视作完成”。后续
[02](02-mla-shared-kv-overlap.md)解除阻塞。

## 最终验收与范围

本票从双 rank 对照扩展为用户批准的 TP1/2/4 C/D 实验。最终收口以
`cd-v1-resume2` 的完整结果、此前组件验证和用户接受为准；下方初始清单保留为
历史设计。真实模型的并行接线与服务验收仍由02/03完成。

- [x] TP1/2/4、集中独立I0、集中共享S0、分散独立ID共七种布局完成三轮测试。
- [x] 大块分工写、等总copy量对照、相同索引top-k2048的完整并发读均有结果。
  local/remote/mixed、batch1/16分开，读取字节不因共享减少。
- [x] 两侧入口493/493并完成收尾和报告收集，保存内容/guard校验通过的检查点。
  经审查复用的旧检查点保留来源，不将续跑声称为一次连续运行。
- [x] 用户回传完整162行CSV，每行60批；带宽/字节口径核对并交付图表分析。
- [x] 用户接受组件结果，决定后续默认每NUMA两个ranks、共享开关独立可配。

**接受的限制：** 所有 `numa_verified=false` 保持原样。驱动映射缺少可观察的
页节点计数；指定NUMA配置、物理贡献和性能数字不构成物理页落点已验证。
最新C/D只覆盖Graph/48，不把旧eager、HCCL或materialize数据拼作本轮样本；
旧组件trace未逐项人工复核的内容不记为已复核。用户本次接受关闭不伪造这些
证据，也不替代02真实模型的内容、发布、Graph与生命周期验收。

## 原始范围与决定（历史）

- 首版 BF16 MLA，每组两个 ranks；P/D 分组一致，各自指定本机 NUMA。
  独立基线为两个原 P_i/D_i session，共享候选为一个四 participant session。
- 优先采用每侧一个完整 allocation owner、另一个零贡献成员。两个 ranks 都
  直接映射 owner 的完整 KV，owner 不代理 copy；P/D 容量可不同。
- 复用真实生产 copy 和现有独立 gate 的环境、Graph、报告与清理机制。
  对照矩阵和计时合同见[执行设计](../execution-design.md)。
- 组件带宽结果不能替代 TP16 真实模型性能。沿用 3% 的服务验收容差，不另设
  每个 copy 大小的 3% 硬门槛；带宽瓶颈由本阶段报告暴露并审阅后再进入02。
- 容量只需覆盖有效实验，不冲顶 3M 或 2T；不重开历史 BIOS/NUMA 调查。

## 原始验收设计（历史）

此清单记录最初设计；最终收口范围与接受限制以上文为准。

- 同一组 NPU/NUMA 可切换 independent/shared，记录真实物理贡献、分配落点、
  stride/对齐/probe 开销及 SDK/driver/kernel 版本。VA 保留量不算物理副本。
- 验证四 participant join、零贡献成员的 reach/status、稳定设备映射、drain
  和关闭；不将参考源码可行性当作部署 SDK 的实测成功。
- 两个 ranks 同时向共享池写互斥分片，完整读回独立预期内容，覆盖尾部和边界；
  两个 readers 各自从同一份存储得到完整 KV，非目标区未被覆盖。
- 等逻辑写入对照：独立池每 rank 写全量，共享池合计只写一份；另交付等 copy
  请求字节数的双 writer 对照，分开报告去重收益与共享争用。
- 双 reader 对照保持每 rank 的完整 selected KV，覆盖本地 D、远端 P、mixed，
  相同有效 top-k、索引分布与命中率；主要带宽 case 有真实有效数据，不能仅测 padding。
- 组件对照本地 current-fill 与历史 UniDexCopy 串行／独立并行填入同一
  selected buffer，验证互斥目的位置、完整结果与 selected-ready 时间；
  不把 BM 写回完成加到 selected-ready 的依赖中。
- 覆盖 bulk 和 decode 小粒度、单/双访问者、现有写读顺序及新写/历史读重叠。
  记录分片、mask、copy block_dim 与当前可实现候选；单 token 只有一名 writer
  的策略如实报告，不以 bulk 结果冒充每步双 writer 性能。
- 暖机后多轮交替 A/B；确认双 rank 同时执行并完成所有相关 device streams。
  分别报告每 rank 设备延迟、同机组完成包络、启动偏差、P50/P95、有效字节与
  原始样本；逻辑速率、copy payload 带宽与实测总线流量不混用。
- eager 与 Graph 分开验证，覆盖有效/无效项、重复 replay、请求/索引/slot
  切换，内容正确；用延迟 writer 验证本地 current 供给与已有 TP 发布顺序。
  组件通信次序不自动证明真实服务的 TP hook 已正确接线。
- 测试入口自动完成 setup、readiness、测量、结果校验、报告收集与自身进程
  清理；多机每机一个入口，日志在 `/home/cryang/log/`，失败返回非零。
  单侧或 eager-only smoke 明确标注，不能当完整组件验收。
- 按[阶段交付流程](../../ascend-mempool/verification.md)完成实现核对与用户
  NPU 验收；用户审阅去重、并发读写瓶颈及进入服务的候选后确认本票完成。

## Verification

Mac 做适用的静态与 CPU 检查，覆盖布局、分片、计时字节口径和配置错误的外部
行为。NPU 由用户运行；提供基于实际脚本的每机命令、容量/NUMA/NPU 配置、
预期报告、失败判据和日志清单。纯 copy 时间与含 metadata/cache 的 materialization
时间分别给出。缺少双 rank 读取对照时，本票不得仅凭写入去重关闭。

## Comments

2026-10-12：依据用户对第一阶段的反馈建立。需求就绪，尚未实现；没有本票
NPU 结果。验收后解锁[02](02-mla-shared-kv-overlap.md)。

同日补充：用户要求 current KV 填入 selected buffer 与历史 UniDexCopy 并行。
组件补对应 copy 对照；真实跨层写回、模型 TP hook 和末层收尾由02接入验收。

2026-10-12 实现交付：新增独立包入口
[`run_numa_sharing.sh`](../../../ascend-mempool-test/scripts/run_numa_sharing.sh)，
运行说明与逐机命令见
[`NUMA_SHARING.md`](../../../ascend-mempool-test/NUMA_SHARING.md)。
代码提交：`2a2007e82f`（`cryang/dev/mempool`，15个实现/测试/说明文件；原有设计
文档改动保留在工作区，没有纳入该提交）。
实现两个独立P_i/D_i session与四participant共享owner对照，含真实copy、
物理贡献/NUMA/映射/probe、fresh LEAVE→JOIN、完整读回与guard校验；按消费者侧
分开采样，另侧保持加入且不提交设备操作。顺序基线从BM读取已发布current，
重叠路径从本地提供current；包含单/双reader、等逻辑/等字节写入、固定cache命中率、
eager/Graph、延迟writer发布检查、独立trace及自动报告/清理。

Agent在Mac实际执行：独立包完整CPU单元测试253项通过（其中本次新增18项）；
strict mypy通过11个相关源文件、ruff检查通过、13个新增Python文件通过3.9语法解析，
shell语法与安装CPU torch环境下的`--describe`通过。CPU测试日志：
`/private/tmp/numa-sharing-cpu-tests.log`，计划快照：
`/private/tmp/numa-sharing-plan.json`。Spec与Standards并行审查后已修复四reader争用、
顺序基线缺少current BM读取、MF端口rank偏移、Graph重复host wait等问题；
复核无剩余阻断，补充矩阵完整性、重入事件和并发效率的CPU回归覆盖。

**等待用户执行 NPU 验收。** 尚无本票实际BM/NUMA/Graph/HCCL结果，不能根据CPU
检查勾选硬件验收项。完整模式与smoke分开标记，NPU日志保存到
`/home/cryang/log/numa-sharing/<run-id>/`；用户核对实现、两侧报告和trace并确认后
才关闭01并解锁02。当前保留`ready-for-agent` / `open`。

2026-10-12 用户首轮NPU反馈（`run-1`）：两侧pull显示Already up to date；P为
`10.120.72.31`，D为`10.120.72.32`，两侧输入devices=`0 1`、NUMA node=`0`。
两侧均启动两个worker；在`round-0/independent`，D父进程报告`member-0 exited
with 1`，P父进程收到对应`peer failed`并终止。此输出尚无worker traceback，
不能确定根因、设备/NUMA拓扑是否匹配或是否到达具体copy步骤。
原始日志位于`/home/cryang/log/numa-sharing/run-1/{prefill,decode}/`；下一步读取
两个worker的`.log`及`report.json`提取最初异常。未通过本票硬件验收，保持open。

同轮补充日志：P/D四个worker均在`sharing_worker.py`的NUMA落点门禁处抛出
`RuntimeError: cannot prove owner NUMA placement`。之前的owner分配日志均为
`HalMemCreate ret:0 numa:0 size:1073741824`，P两层各写入150994944 bytes，D两层
各写入301989888 bytes；已越过映射及probe检查，尚未开始benchmark矩阵。
不能由此错误推断物理NUMA错误；待读取各worker保存的`mapping.numa`原始证据，
区分实际节点不符与`/proc`无法观测驱动映射。日志中的早期远端GVA未就绪错误
之后出现成功import/mmap，未导致本轮最终退出。

同轮NUMA报告确认：P0/P1的owner地址为`0x280040000000`，maps匹配
`/dev/davinci_manager`，numa_maps为`280040000000 default file=/dev/davinci_manager`，
`resident_pages={}`。当前驱动映射没有可见的节点页计数；这是证据不可观测，
不能判断物理落点错误，也不能证明NUMA验收通过。

修复测试门禁：仅两份/proc记录都匹配该驱动映射且没有驻留页计数时，允许继续
采集测量，仍保留`placement_verified=false`。完整模式若任一owner未验证，
两侧完成矩阵、报告交换及清理后输出`COMPONENT_RESULTS_PENDING_NUMA`并返回2；
观测到错误/混合节点、映射缺失或记录不匹配仍失败。未更改SGLang生产代码。
精确run-1 fixture已复现旧异常后通过；补充缺失/错误地址/错误file匹配的red→green
回归。Mac完整CPU测试259项通过，strict mypy 6个相关源文件通过，ruff lint/format
及diff检查通过；Spec/Standards复核无剩余阻断。CPU日志：
`/private/tmp/numa-evidence-cpu-tests.log`。等待用户以相同配置、`run-id=run-2`
重测NPU；01仍open，未解锁02/03及原04。

修复提交`33a66371fd`已push到`origin/cryang/dev/mempool`；仅包含6个独立测试包
文件，原有未提交设计文档仍保留在工作区。两侧pull后执行run-2，报告路径为
`/home/cryang/log/numa-sharing/run-2/{prefill,decode}/summary.json`。

run-2用户反馈：P父进程报`member-0 exceeded 3600s`，D父进程报`timed out`。
四worker最近完成`48/write_then_read-1/consumer-1/graph`，随后均进入
`48/write_then_read-16/consumer-0/eager`且无PASS。D两worker在correctness的
`copies-drained`四方同步等候，收到清理SIGTERM；P未提供终止时调用栈。
此前大量eager/Graph case已有PASS，但第一轮independent未完成、shared未开始。
代码核对：Processes.wait的3600s限制整个worker等待周期，不按case进度重置；
仅凭最后BEGIN不能证明该case独自阻塞3600s。需用现存samples.json的采样跨度、
修改时间与failure.json时间差区分累计耗尽预算与长时间无进展，尚未确诊设备根因。

run-2时间证据：P0/P1各450条保存样本，采样跨度1712.2秒，末次samples保存到
failure.json分别1867.4/1867.3秒；最后一项之后约31分钟无PASS，不能解释为
正常矩阵持续推进到总预算耗尽。P侧缺少阻塞时调用栈，尚不能断定copy、HCCL、
ExternalEvent或设备同步中哪一步阻塞。

新增`--diagnose-write-read`诊断入口：一次independent，仅保留write_then_read-1→16、
双consumer、eager/Graph，默认3 samples/64 repeats；命令指定48 blocks、timeout300。
诊断步骤写UTC时间与progress.json，60秒无步骤推进后自动重复打印Python栈，
不改变copy/HCCL/设备同步顺序；成功为DIAGNOSTIC_PASSED而非组件验收，comparison为空。
暂留诊断标记`[DEBUG-numa-sharing]`以收集下一次实机证据，根因查明后再清理。
Mac完整CPU回归262项通过，strict mypy6源文件、ruff、describe与diff检查通过；
诊断CLI合同测试先red后green，另验证device-sync失败边界与实际子进程watchdog输出。
这验证了诊断工具，未在Mac复现或修复NPU阻塞。Spec/Standards复核无阻断。
CPU日志`/private/tmp/numa-diagnostic-cpu-tests.log`，计划`/private/tmp/numa-diagnostic-plan.json`。
等待用户run-3-diag；诊断若通过，只能说明缩小场景未复现，不能关闭01。
诊断提交`984ba9fbf1`已push到`origin/cryang/dev/mempool`，仅修改独立测试包。

用户run-3-diag反馈：P/D均PASS round-0/independent并输出DIAGNOSTIC_PASSED。
缩小后的48 blocks写后读1→16、双consumer、eager/Graph序列未复现run-2阻塞；
不能据此排除前置用例状态或偶发因素，也不能宣称原完整矩阵通过。
新增`--diagnose-independent`，执行一次原完整independent矩阵，保留24→48、
原case顺序及默认采样参数，同时启用步骤记录/60秒无进展自动栈；仍不跑shared、
后续轮次或delayed profiler，不修改copy/HCCL同步。交付run-4-diag命令使用
timeout2400（worker预算40分钟，另需清理）；仍为诊断，不验收、不关闭01。
Mac CPU263项通过，strict mypy6源文件、ruff、describe和diff检查通过，
Spec/Standards复核无阻断。CPU日志`/private/tmp/numa-independent-cpu-tests.log`。
整轮诊断提交`08d1fd4b1e`已push到`origin/cryang/dev/mempool`。

run-4-diag用户栈：再次进入48/write_then_read-16/consumer-0/eager的首个correctness，
UTC15:25:00.461863 launch、15:25:00.462730 device-sync，随后连续10次60秒
watchdog栈均在torch_npu/npu/utils.py:synchronize，由check_work调用；未进入
verify-selected。这些Timeout文本是周期栈转储，不是每分钟一次新失败。
结合run-3缩小序列通过，调查前置序列相关的stream/event状态。

发现可复现的计时器提交死锁：Pipeline.run先向observer入队external wait再invoke。
官方ProcessGroupHCCL.cpp从stream池分配并缓存通信stream，NPUStream.cpp按池复用；
若observer与之前缓存的HCCL stream重合，observer先等待main的selected信号，
HCCL在同队列中排在wait之后，main又等待HCCL完成才能记录selected，形成环依赖。
证据链接：https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/distributed/ProcessGroupHCCL.cpp
及https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/core/npu/NPUStream.cpp。
此条件与实机现象相符，但尚未取得部署SDK的底层stream身份，不能确认就是实机根因。

回归调用实际Pipeline.run，以确定性队列执行main/HCCL/observer的record/wait关系。
旧实现alias场景在device synchronize报循环等待（无alias对照通过）；修复为producer
先提交、observer后提交，带/不带计时和连续3次提交均通过。不改KV写入/HCCL/读取
依赖，仍等observer消费后才允许下次reset。报告和说明明确selected观测值包含
host提交及observer调度延迟，是上界，重叠需由trace核对；CPU模型不证明NPU Graph。
Mac完整CPU265项通过，strict mypy5源文件、ruff lint/format、diff检查通过。
日志`/private/tmp/numa-observer-order-cpu-tests.log`；保留诊断以等待run-5-diag原序列
重测，实机原阻塞尚未证实修复，01继续open。
Spec/Standards复核无阻断；修复提交`be5844713c`已push到`origin/cryang/dev/mempool`。

2026-10-13 用户run-5-diag实机反馈：按修复版本交付的命令，P/D均输出
`PASS round-0/independent`与`DIAGNOSTIC_PASSED`。配置devices0/1、NUMA0、
block-dims24/48、timeout2400，两机地址沿用10.120.72.31/32；P worker PID
185215/185216，D worker PID371570/371571。本次完整independent原序列通过，
越过前两次固定阻塞位置，支持producer先提交修复有效；未取得底层stream身份，
未独立读取summary中的代码SHA，也不据此声称所有硬件场景通过。
结果路径`/home/cryang/log/numa-sharing/run-5-diag/{prefill,decode}/summary.json`。
尚待shared、三轮交替对照、delayed-publication/profiler和NUMA证据验收。下一步
交付run-6-full，去掉diagnose参数，保留24/48、默认3rounds/3samples/64repeats，
每组worker超时3600秒，日志仍在/home/cryang/log。NUMA不可观测则完整采集后
返回COMPONENT_RESULTS_PENDING_NUMA/2。01继续open，不解锁02/03及原04。

run-6-full用户反馈：两侧round-0/independent PASS；shared四worker的benchmark
到最后48/zero-valid/consumer-1/graph均PASS。之后delayed_publication中D member-1
抛出`delay fixture did not expose selected-ready before writer completion`；P0/P1/D0
在delay done同步收到worker failed abort，为连带失败，不能将其当最初异常。
共享映射/成员与单轮矩阵已越过，但delayed检查与三轮验收尚未完成。

定位旧观测缺陷：selected_ready.synchronize后writer_done.query只反映host查询
时刻状态。若设备selected先完成但host晚调度到writer完成之后，会错误拒绝。
实际delayed_publication函数CPU边界复现selected=2ms、writer=5ms、query=True
时抛出同一断言。此模型证明误判可能性，不能确认实机一定有正向设备间隔。
候选还包括延迟量不足、真实依赖串行，需设备时间戳区分。

修复publication专用ready/selected/writer事件启用计时；eager完成drain和读回后
比较同一ready基准的两个设备时间，要求member-1的writer_minus_selected_ms>0。
Graph仍只验证读回并采trace，不读取旧capture时间。每项结果写入
delayed-publication.json后再执行正间隔门禁，实际无间隔仍失败。
新增--diagnose-publication：一次independent/shared，仅setup/映射重入/staging、
延迟发布eager/Graph及trace，跳过性能矩阵；DIAGNOSTIC_PASSED非验收，summary保存
publication记录，父进程核对完整4项及读回/设备时间。保留512延迟量以先检验观测误报。
交付run-7-publication，timeout600为每模式worker预算。原run-6数据保留，不能据
首轮样本声称三轮性能验收通过。
Mac完整CPU268项通过、strict mypy6源文件/ruff/describe/diff检查通过。host晚观测
回归先red后green，反向/零/无效时间及缺失/重复报告仍fail。CPU日志
/private/tmp/numa-publication-cpu-tests.log。等待NPU重测；01继续open。

- 2026-10-13 final review: eager publication uses same-device Event.recorded_time() integer microseconds (not cross-stream elapsed_time); timing disabled for Graph. CPU 268 tests, mypy six source files, ruff/isort and both spec/standards reviews passed. Short independent+shared publication diagnostic pending user NPU execution; actual run-6 overlap remains unproven. Ticket remains open.

2026-10-13 用户反馈 run-7-publication：P/D 均 PASS round-0/independent、
PASS round-0/shared、DIAGNOSTIC_PASSED。按交付命令 devices 0/1、NUMA 0、
--diagnose-publication --timeout 600，交付版本 622c93f88e（未独立读取实机 SHA）。
两种池的 eager 正向发布间隔门禁、eager/Graph 读回和 trace 生成均通过入口检查；
尚未收取原始时间差或人工审阅 trace，不据此追溯断定 run-6 的真实事件先后。
报告 /home/cryang/log/numa-sharing/run-7-publication/{prefill,decode}/summary.json。
下一步 run-8-full：去掉 diagnose，block-dims 24 48、rounds 3、timeout 3600，
恢复完整交替性能对照。01 仍 open；三轮性能、NUMA 证据及 trace 验收尚待完成。

2026-10-13 用户反馈 run-8-full（24/48、3 rounds、timeout3600）：P/D 均完成
round-0 independent/shared 与 round-1 shared/independent，四组均 PASS。
round-2 independent 失败：P launcher 报 worker244514 exit1，D 报 member-0 exit1；
仅凭 launcher 输出无法确定首发 worker、阶段或根因，等待两机该组四份日志及
report/progress/delayed-publication 证据。round-2 shared 未启动；不得当作三轮
完整性能验收。保留已有样本，01仍open，当前不修改代码或要求全量重跑。

2026-10-13 run-8完整日志：round-2 independent四worker的benchmark均走完；
首发为D member-1 delay/eager/0，selected85.730ms、writer85.500ms、gap-0.230ms，
publication_readback_passed=true，closed=true；P member-1 gap+2.105ms，其他abort
为连带退出。member-0不注入延迟，不要求正间隔。CPU执行真实Pipeline.launch的
host/device提交模型复现旧顺序gap=-0.23，先提交selected后提交延迟writer变green；
不证明实机唯一原因。修复仅publication+delay fixture，不加selected→writer设备
依赖，正常benchmark仍writer先提交（回归覆盖），正间隔门禁/发布读回保持。

用户要求进度条并复用前两轮。新增父进程10秒增量日志进度（较慢worker进度、
round/mode、当前阶段、耗时，不进计时区间）；--resume-from run-8-full
--resume-round 2以新run-id复用前四组、完整重跑第三轮independent/shared。
严格核对旧plan/config/精确622c93f88e源指纹及受审计目标实现指纹，成功closed、
完整样本、finite一致计时、非空trace、每member环境；新worker初始化BM前对照
SDK/驱动/CANN/内核/hostname/设备/affinity，差异失败。两机先互认复用清单。
原目录保留，新summary记录来源与时间断点，不吞失败样本、不算连续运行，
NUMA待确认仍exit2，01继续open。交付run-9-resume，24/48、3rounds、timeout3600。
Mac完整276 CPU测试通过，mypy8源文件、ruff/isort/diff检查通过，Spec/Standards
最终复核无阻断。日志/private/tmp/numa-resume-cpu.log。实机修复与续跑待用户验证。

交付提交424152677f，推送origin/cryang/dev/mempool。

2026-10-13 用户反馈 run-9-resume P 终态 COMPONENT_RESULTS_PENDING_NUMA，路径
/home/cryang/log/numa-sharing/run-9-resume/prefill/summary.json。按入口代码，此输出
在P/D报告收集和最终barrier之后出现，支持续跑及合并流程完成；尚未直接读取
summary数值和来源，不宣称共享性能优于独立池。NUMA仍pending，退出码2为设计行为。
下一步从P侧summary.json与已收集decode-summary.json审阅性能、发布间隔、NUMA
证据和trace；不要求再次全量重跑，01继续open，下游未解锁。

2026-10-13 用户回传run-9-resume两侧Graph/block_dim48的43项P50及共享/独立比值。已制作[性能解读页](../visuals/run-9-performance/report.html)，保留86项对照、4图及CSV。2049行等逻辑写入P50下降37.2–42.0%，等字节大块接近持平；双reader变化-1.3–+3.3%；current_parallel相比同池serial组耗时增加约19.8–62.4%。后者不是selected-ready比较，待查原报告、P95/逐轮/trace；仅摘录不可确认带宽瓶颈或服务性能。NUMA仍pending，01保持open。

2026-10-13 用户追加TP4组件对照，并明确确认P=10.120.72.31、D=10.120.72.32，
两侧devices 0/1/2/3同属NUMA0。扩展仍归共享01，不启动02/03服务集成。
独立模式每侧四池，对照每侧四rank直接共享一个owner；八participant协调，六名
零贡献follower逐个drain/leave/rejoin/probe；本机HCCL四成员。四reader读取相同
selected KV，各自得到完整结果；等逻辑写入共享合计L、独立4L，等字节对照均4W。
用户追问2049×4：明确这仅是等字节诊断的互斥地址范围，正常共享逻辑KV仍2049行。
TP4默认P/D均16384tokens、4layers、16slots、576维BF16、topk2048；额外512B probe，
每owner物理贡献2GiB，每侧独立8GiB/共享2GiB。P容量扩大容纳8196行+guards；4层
覆盖四个layer owner，每个timed case仍一层。不将不同容量的历史TP2绝对延迟当作
固定形状扩展效率。53cases覆盖eager/Graph、24/48、3轮AB/BA/AB及四个单reader。
发布诊断延迟member3且active4，保证延迟writer有真实分片。四成员进度/统计/续跑
完整性与宽度隔离已接入；历史TP2报告不可混入，旧审计迁移目标指纹不刷新。
生产SGLang代码未改。逐机命令、端口19400–19438和容量见ascend-mempool-test/NUMA_SHARING.md。
Mac实际检查：317项完整CPU回归通过（/private/tmp/numa-sharing-tp4-tests.log）；
strict mypy Linux目标13源文件、ruff、isort、Python3.9语法、bash语法与diff检查通过；
--describe已验证TP4计划。新增汇总验证后的runner回归也通过。
等待用户运行run-10-tp4，timeout7200为每组预算，不是预计时长。NPU BM/Graph/HCCL、
NUMA实测与trace审阅仍待执行；NUMA缺计数继续输出pending，不关闭01或解锁下游。

TP4交付提交 `bb53ac0f33` 已推送 `origin/cryang/dev/mempool`。Spec复核0项需修复；
Standards复核0阻断，两处旧four-participant/worker注释已修正。仅提交20个独立
测试/说明文件，既有本地设计文档改动未随提交。硬件验收待用户运行。

2026-10-13 用户进一步收窄本次TP4为仅Graph、block_dim48。新增--engines选择并贯通
性能矩阵、发布读回/trace、进度分母、报告完整性和续跑证据校验；Graph-only不要求
eager时间戳或trace，保留capture前预热；scope=selected_component，不能声称覆盖
eager/24。仍53cases、三轮AB/BA/AB，改用新run-id run-10-tp4-graph48。
Mac87项sharing CPU回归、strict mypy13源文件、ruff/isort/py39/diff和describe通过。
等待用户NPU实测；NUMA/trace人工验收仍pending，01不关闭。

Graph/48筛选交付 `53bcce4eed`，已推送origin/cryang/dev/mempool；两侧更新后运行
--engines graph --block-dims 48，run-id=run-10-tp4-graph48。

2026-10-14 用户批准 C/D 重新设计后实施交付：新增 `run_numa_cd.sh`，详见
[运行说明](../../../ascend-mempool-test/NUMA_CD.md)。真实 TP1/2/4 共七组布局；
集中节点0，独立分散节点0/2及0/2/4/6，CPU固定node0且各rank互斥CPU子集。
每池固定一层、16×65536×576 BF16，owner实际2GiB。主写L=2048/8192/32768行：
独立每rank完整L，共享每rank连续互斥L/N；等总copy量控制均为整组L。
D为每rank完整top-k=2048、batch1/16、相同随机索引、local/remote/mixed来源；
一次只施加一侧的读写压力。只跑Graph/48，无HCCL、materialize或旧publication矩阵。
自动先完成七组映射/probe/rejoin/NUMA预检查；三轮TP及模式轮换，20批×64replay，
前后完整内容/guard校验、2049尾部校验。BF16写marker限制在[-256,-1]，避免跨256后
精度舍入合并相邻token标识。每rank device时间与整组host envelope分开报告。
校验后的每个用例原子保存；只在两侧所有rank checkpoint齐全、代码/配置/环境
一致时复用。保留不合格启动尝试、进度/ETA及pending NUMA，P自动收集D汇总CSV。

Mac实际执行：342项完整CPU回归通过（`/private/tmp/numa-cd-cpu-tests.log`），其中新增
20项覆盖分片守恒、missing writer/guard、完整共享读取、真实TP1本地cohort、Graph
协议模拟、逐用例续跑、双机七组预检查/调度/报告收集、错误采样拒绝与进度。
本次9个Python源码严格mypy通过；测试目录ruff、本次isort、shell语法、describe通过。
广域mypy尝试另暴露未改动sharing_pool基类导入/verify_resources标注问题，未将其修复
混入本次提交；本次修改文件单独检查通过。未访问NPU主机；Graph和不同NUMA实际
落点待用户执行。生产SGLang代码未修改，01仍ready-for-agent/open，02/03及原04
保持原硬件验收依赖。

2026-10-14 本次代码提交为 `61e22c8603`（17个文件，仅ascend-mempool-test）。
推送到 origin/cryang/dev/mempool 被自动审批拦截：审批要求用户明确此次外部仓库目的地
与发布payload。当前仅本地commit；尚未push，等待用户明确确认推送该commit至
https://github.com/ITCJ/sglang.git 的 cryang/dev/mempool。

随后用户明确确认“推送到该仓库和分支”。`61e22c8603` 已成功push至
`https://github.com/ITCJ/sglang.git` 的 `cryang/dev/mempool`，远端同步检查0 ahead/0 behind。
当前等待用户运行 `cd-v1-full`；本次测试目录无未提交改动。

2026-10-14 用户反馈 cd-v1-full 在305/493、round-1/4-I0、
read-remote-random-1-side-0 失败。D member0/1首因均为
`coordination exceeded 100ms launch window`，member2/3为cohort级联abort。
此时活跃consumer是P，D闲置；100ms是控制面启动目标，不是设备copy超时。
CPU确定性过期回复测试先复现相同异常，再修复aligned_start：错过目标后继续
进入采样汇合；活跃rank的真实启动偏差和区间重叠仍由原group_sample筛选，
C/D现有最多3次重试不变。未改变负载、kernel、计时边界或验收阈值。
仅允许61e22c8603精确source hash跨版本续跑到本次经runtime digest锁定的修复。
原checkpoint保留测量版本contract.code，仍检查所有rank完整性、配置、环境、
内容校验及NUMA证据；未知旧版本/任意其他runtime变动不允许跨版本复用。
Mac：347项全CPU测试通过（/private/tmp/numa-cd-launch-full-tests.log），
包含真实八worker协议中过期闲置rank回复及原版本checkpoint兼容/拒收回归；
9文件严格mypy、ruff、isort及diff检查通过。NPU恢复运行尚待用户执行，票仍open。

2026-10-14 cd-v1-resume / round-0/4-S0 收尾卡住：两侧24个case均PASS REUSED，
P四worker与D member1正常退出；D member0/2/3仍存活并重复CAS/QueryAlive及
重连10.120.72.31:19402。P member0于01:24:17先完成Store Shutdown，而D仍需
Store完成leave，因此缺少pool关闭后的全员同步；原drained只证明设备工作排空。
P父进程等待对端closed时没有progress回调，导致停留在旧setup 121/493。

修复：cohort.shutdown在设备drain、pool关闭、非Store客户端uninitialize之间
执行全员同步，所有Store owner最后uninitialize。共享Store为P member0，独立
各P member均为Store owner。cleanup错误携带阶段/错误并令整组失败，不跳过失败
验收；最终report和日志记录cleanup_phase。C/D父进程在等待对端收尾时继续刷新
progress，并从观察到收尾开始设置120秒预算，失败后仍只清理本次process groups。
保持原case计时、数据路径和并发验收不变，旧61e22c8603及0d000b262a精确source
可迁移至此次审查锁定的runtime digest；checkpoint原contract.code保留。
建议以覆盖更完整的cd-v1-full为来源，以cd-v1-resume2为新run-id恢复。

Mac反馈环：真实八worker TCP协调+慢D退出的SDK边界替身，旧顺序先复现
“P Store shut down while D was still leaving”，修复后通过；亦覆盖TP1/2独立
Store、failed work、drain/pool/client关闭异常禁止提前关闭Store、父进程收尾进度
与120秒超时。完整354项CPU测试通过（/private/tmp/numa-cd-shutdown-tests.log），
10源码严格mypy、ruff、isort及diff检查通过。真实NPU收尾修复效果仍待用户续跑验证。

2026-10-14 用户反馈硬件续跑 cd-v1-resume2：P与D均493/493，最终组
round-2/2-S0 closed、PASS（该组reused 0），两侧elapsed约18.3分钟，均输出
COMPONENT_RESULTS_PENDING_NUMA。修复提交d2c999b4fa已push；本次完整矩阵（含
原cd-v1-full中经审查复用的检查点）、双机收尾及报告收集已完成。
报告路径 /home/cryang/log/numa-cd/cd-v1-resume2/{prefill,decode}/summary.json；
P另有decode-summary.json及combined-results.csv。尚未读取实际性能数值，不记录
任何速度结论；实际NUMA落点仍pending，跨NUMA对比仅表示请求的分配配置，不能
当成物理落点因果证据。01保持open，后续等待性能汇总审阅及NUMA验收。

2026-10-14 用户回传本轮完整combined-results.csv，已核对162行唯一side/topology/case、
全部每行60批、预期字节量与带宽关系一致；所有numa_verified=false。原始CSV与
可复现计算、四张Matplotlib图、完整P/D配对表保存在
[cd-v1-resume2报告](../visuals/cd-v1-resume2/report.html)。CSV SHA256为
89a8d718c11f59e8818cb5a5bfbcdbc224e59a0e170f06bcb1c0c4c6bb0aa6d4。

36MiB逻辑主写：2-S0相对2-I0组P50降低50.7%–51.2%，4-S0相对4-I0降低约74.9%
（约3.98倍完成速率）。TP2/TP4共享分别约0.529/0.522ms。等实际copy量时，TP4的
I0/S0均约0.522ms，差异小于0.1%；主写改善主要来自删除重复写入。4-ID主写约
0.580ms、合计有效copy约260GB/s；等总量36MiB时约0.159ms、约237GB/s。

batch16同索引完整topk2048读：4-S0与4-I0的local/remote/mixed组P50差异不足0.4%。
4-ID本地约0.431ms、合计约350GB/s，I0/S0约1.338ms/113GB/s；ID相对I0完成速率
本地约3.10倍、远端约1.58倍、mixed约1.91–1.93倍。读取实际字节仍按TP增长。
小batch的P侧4-S0 mixed改善约17.1%，D侧却慢约0.9%；D侧2-S0 batch16 mixed的
P50改善9.2%，P95只改善约1.5%，不将单侧/分位数例外概括为稳定收益。
只收到汇总CSV，不能推导置信区间；未混入旧实验。NUMA因果与真实模型服务性能
未证明，票保持open。此次只新增结果报告与本地记录，未修改benchmark或生产代码。

2026-10-14 关闭记录：用户在查看C/D结果后明确接受01完成，默认每NUMA两个
TP ranks，并要求通过共享开关按NUMA分配结果合池。State改为closed，解锁02。
本次只更新设计与本地tracker；未修改测试原始状态或重新运行NPU。
证据：cd-v1-resume2两侧493/493、完整CSV及[结果报告](../visuals/cd-v1-resume2/report.html)。
交付测试代码为d2c999b4fa，含61e22c8603/0d000b262a受审查复用的采样来源；
未独立读取实机git SHA。主机P=10.120.72.31、D=10.120.72.32，NPU0/1/2/3，
请求NUMA列表0/2/4/6、CPU node0。日志位于
`/home/cryang/log/numa-cd/cd-v1-resume2/{prefill,decode}/`。
按此前交付入口，两侧分别运行 `run_numa_cd.sh --role prefill|decode --run-id
cd-v1-resume2 --resume-from cd-v1-full --devices 0 1 2 3 --numa-nodes 0 2 4 6
--cpu-node 0`。原始物理NUMA未验证状态保留，不能据本票关闭宣称已通过NUMA页验证。
