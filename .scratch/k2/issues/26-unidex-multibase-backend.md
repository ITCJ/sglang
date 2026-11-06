# 26: UniDex 多基址统一后端（基础迁移，行为不变）

**What to build:** 用通用 `unidex_copy_multibase` 统一五类操作：主 KV / Index K 写回、swap-in 物化、HBM 缓存回填、slot-map 写入、prefix load_back gather。单基址是多基址表的一项特例；所有保留的旧 UniDex 调用及兼容分支接入同一后端，不保留原单基址算子的调用。当前 fill / admit 继续使用既有 SDMA 批量执行器及完成契约。先一对一替换、保持调用结构并回归一次，再合并 swap-in 并回归一次。多个 DRAM 区域一次读是必需项；两条 HBM 来源合并是优化，出现问题可以暂缓。当前业务放置、写者分工、字节、回填顺序和控制协议不变，25 再改变双写及 publish 语义。

**Blocked by:** [13 观测](13-k2-m1-observability-mode.md)、[18 页粒度 alive](18-k2-s0-page-granular-alive.md)，均已按用户约定范围关闭。[14](14-k2-m2-bandwidth-characterization.md) 已关闭，其 SDK 远端写结果仅作对照。Blocks [25 双写](25-k2-p1-dual-write.md)、[21 跨层预取](21-k2-w1-cross-layer-prefetch.md)、[22 热缓存与读合并](22-k2-w2-hot-cache-and-coalescing.md)；主线顺序 **26 → 25 → 20**。

**Status:** ready-for-human

**State:** open

详细接口、统计与验收约定见 [26 方案](../ticket26-plan.md)。调用点基线见 [完整调用链复核](../unidex-migration-audit-20261106.md)。本票不把交接中的普通内存 Graph 结果当作双机共享池验证。

## 验收清单

- [ ] 五类操作及保留兼容路径全部使用多基址后端；fill / admit 的 SDMA 路径不变。交付调用点清单及 `rg` 结果，生产和验证入口的旧算子调用为零，历史文档/负测试引用单列。
- [ ] Graph 捕获前建全 25 所需基址集合及固定容量页表、任务表。混合读覆盖远端 8 个 prefix 段、本机 D alive、两个 HBM 源；双目标写覆盖远端 D alive、本机候选页及两 rank 按页分写。25 只改寻址表内容，不增加捕获地址集合。
- [ ] 报告 src / dst 基址容量、实际活跃数、源码上限与已测上限；写清 kernel 区间表与设备页表的转换、副本条目预留及版本校验。11 个读源的后续形状和保留 P alive 的 12 槽集合均需验证，不能沿用 8 基址证据冒充。
- [ ] 用新 kernel 重测 4K / 32K / 128K tokens 的一层远端写与双目标写；各含有 / 无 D 本地读，另覆盖 16 rank 同时写。归档吞吐、延迟、D 读干扰、计算重叠及完成信号；写者确认后 D 立即读回比对。按 14 格式归档，保留旧路径及数值。
- [ ] 25 开工前确认 kernel 远端写的可见性契约及一层计算隐藏结论；14 的关闭不能替代这两项。未测到计算窗口则明确未知，不解锁 25。
- [ ] 迁移前后同负载按源读行数与字节一致。回填在物化完成后从 selected HBM 读取，DRAM 重读回填字节为零；当前 token 的 DRAM 读取行数恒为零。“同一源行两条任务”重复读只用于 HBM 源。
- [ ] 使用统一字段对比 T_step、每层物化时间、按源行数和字节。整次物化时间作为暴露 miss IO 的上界；不伪造融合后的分源时间。可选分源诊断与正式融合结果分开；21 / 22 使用同口径对照。
- [ ] 保留 12 的 P 按页分写、D 特征分写、Index K 单写、prompt / decode 边界、当前 token、mask 与 padding 用例；保留 18 的固定页表、换页、零有效行、图重放及过期页表负测试。
- [ ] 保持 06 的每步顺序：同 stream 写回排在本层 all-reduce 前；A / B / C / E、V1–V10 完整输出 ID 一致，字节按 09 的同配置口径核对。真实淘汰长生成须走到回填与 slot-map，并有淘汰证据。
- [ ] 两步各一次回归，只减轮次、不减用例；复用服务、自动准备/验证/清理，失败非零。按实际覆盖区分 CPU、设备微测和服务回归，日志保存 `/home/cryang/log/`。
- [ ] 一个 ticket 尽量一个 commit；Standards / Spec 两轴评审；功能源码、脚本/测试、文档分列交付。用户确认实机验收后关闭。

## Comments

### 2026-11-06：按计划侧确认建票

用户补全六条要求，确认 26 → 25 → 20；尚未开工的 21 / 22 排在本票之后。
14 剩余未测档位后续使用多基址 kernel，不重开已接受的 14，也不将未测项写为通过。
多 DRAM 合并为必需，HBM 两路合并允许暂缓；回填、slot-map 保持独立顺序。
本次仅落票、核对源码并更新依赖和证据边界；没有修改功能实现、运行实机测试或关闭新票。

### 2026-11-06：本地实现、测试与评审交付

已完成一对一迁移和四路物化；fill/admit SDMA不变。固定基址、读写位置表及双任务预留已接线。
新硬件入口覆盖两阶段完整回归、12/11源Graph、两rank/16rank写入与D读回。
阶段1 CPU62项通过；完整套件598项运行，24项环境受限重跑通过，6个既有失败方法保留。
新增内容/字节、第二目标、缓存实际更新与负门禁检查通过；相关22源模块严格类型检查通过。
Standards/Spec初审发现已修复，最终各0未解决。
[交付与命令](../ticket26-delivery.md)、[原始检查](../verification/ticket26-local/)。
未在Mac编译CANN、未运行实机验证；State保持open，等待用户验收，不解锁25。

### 用户授权缩减本轮验收范围

时间有限，两阶段各跑一次 `(64,512)`；其余三种配置不执行。
微测所有档位保留，每档一轮、一次预热、两次测量，无自动重试。
报告明确为单配置覆盖，性能仅作小样本粗测；不能写成四配置完整验收通过。

### 001 实机反馈：测试 arena OOM

32的阶段1 page-first-copy三例通过；31小例通过，主KV测试arena申请10.97 GiB失败，当时仅8.65 GiB空闲。
验证脚本将主KV测试slot由16降至8，arena为5.48 GiB，保留78层、8192 token和超过4 GiB的实际访问偏移。
同步阶段1归档；CPU回归先失败后通过。生产布局不变，实机重测待回传，26仍open。

### 002 fetch Graph捕获失败

页拷贝通过并进入fetch；D边界测试首次gather在capture内创建BaseTable，torch.tensor从Python列表初始化触发同步H2D，报107030。
改为设备empty和fill_，同步阶段1快照。首次调用/形状切换CPU回归先失败后通过，5项相关检查通过。
设备重测待回传；先只重测双机fetch，复用已编译插件，见[ticket26-fetch-retry](../ticket26-fetch-retry.md)。

### 002 fetch重测通过与续跑

用户回报31/32均ALL_CHECKS_PASSED：阶段1双机fetch含Graph捕获/replay通过。未据此关闭26。
新增--phase1-component-run-id复用此前成功页拷贝和fetch-retry JSON，校验通过状态、rank与几何，复制原报告并保留来源路径。
003从阶段1服务回归继续，阶段2与远端写仍完整执行各自约定范围；仅64/512，微测测量两次。2项CPU检查与静态检查通过。

### 003 reference权重OOM：启动前已有设备占用

用户提供预检与启动前两份npu-smi快照：旧SGLang scheduler在每卡占用53252–53412 MiB，总HBM占用56083–56584 MiB；所属作业未查明。
通信初始化净增约0.01 GiB，随后加载权重前仅余8.55–9.12 GiB并OOM；不是此次KV/Index K预算计算导致的权重加载失败。
后续31快照显示无设备进程、每卡约3 GiB占用，表明占用已解除。004继续服务验收并复用002组件证据，26仍open。
新增HBM缓冲的预算记账单独待核对，不用它解释003失败。

### 004：31再次存在启动前占用，32可正常开始加载

用户回传：31在reference启动前，host PID 475550等16个scheduler各占53832–53834 MiB；附带python PID 440115。与003 PID不同，作业归属和是否自动重启尚未确认。
31权重加载前仅余7.96–8.49 GiB并OOM。32启动前无设备进程，权重加载前60.77–61.07 GiB，日志到35/96分片，无独立异常。
不再安排下一轮重跑，先在31宿主机核对占用进程所属容器/作业；不按历史PID直接终止进程。26仍open。

### 005：capacity 启动端口冲突与从失败阶段续跑

005已进入capacity，证明reference/target的请求、阶段检查及清理已返回；完整阶段1尚未通过。
31于06:38:19初始化通信时绑定10000失败（EADDRINUSE）；模型未进入BM初始化。
验收循环漏查P launcher退出，导致32等待BM store到07:49。后续宿主机ss显示10000空闲，不能还原当时占用者。
已修复两侧注册等待期间的非预期退出检测，沿用既有peer错误广播和自身进程清理，容量拒绝引起的预期退出仍允许。
用户要求完成的测试不重跑；新增--phase1-service-run-id，006复用005组件/reference/target，直接从capacity继续。
原报告复制到新目录并保留来源；核对有效配置、模型、包版本、基点与功能源码，只允许验收脚本变化。
当前新增六项定向检查通过，评审修改后三项受影响检查通过；阶段1归档同步且功能源码字节未变，Standards/Spec各0未解决。
功能源码无改动；仅验收脚本、相关测试和交付说明变化。实机续跑待执行，26仍open。
见[续跑说明](../ticket26-capacity-resume.md)、[证据](../verification/ticket26-local/capacity-resume.json)。

### 006：阶段1执行完成，阶段2reference端口绑定冲突

006推进至阶段2，说明阶段1剩余capacity/timeout及汇总已返回成功；阶段2copy/fetch也已返回成功。
阶段2reference启动时，31 TP3在08:34:34绑定19003失败（EADDRINUSE，config store -2009），默认清理P进程；32随后握手连接被重置。
用户事后在31宿主机查询19003，无任何监听/连接/TIME_WAIT记录，历史占用者未知。没有将端口冲突归因于kernel。
新增--start-from phase2与--phase2-component-run-id；007复用006成功组件结果，直接从阶段2reference继续，阶段1不执行。
复用沿用已有passed/rank/geometry验证与原JSON归档；之后照常执行阶段2其余检查、cache gate和两档rank数微基准。
修改仅run_ticket26.py和相关测试/文档；功能源码、SDK与默认退出不变。两项CPU定向检查覆盖双侧编排与组件证据拒绝，静态检查通过。
26仍open，实机续跑待回传；本次没有宣称端口冲突已根治。见[阶段2续跑](../ticket26-phase2-resume.md)。

### 007阶段切换审计：reference端口检查遗漏已复现并修复

007的阶段2reference完成后，target由32报告服务退出137，原始异常栈尚未收到，不能认定本次根因。
用户怀疑上一服务未完全释放或没有等待。代码审计发现：runner会等待自身进程组退出并两侧同步，但reference清理误用target共享布局端口清单。
共享Index K模式只检查BM store19000，reference实际用16个独立store，19001–19015及部分NIC端口漏查；19003在漏查范围内。
新增Runner.stage回归复现“19003仍占用而reference被标完成”；现按实际阶段推导端口，并在有reference的运行预检两套端口。
沿用有上限的TIME_WAIT等待、原进程组清理和SGLang退出；没有加固定sleep或改变SDK。四项定向CPU检查通过，阶段1快照同步。
此修复解决确定的验收检查缺口，不证明历史冲突来自旧worker，也不证明007失败已解决。
已完成阶段保留；继续等007的32 target异常栈，26保持open。

### 007 target错误与008单段重试

用户回传32 target：Graph准备前各rank余29.74–30.04GB；启动前npu-smi无进程。10:35:55开始报aclrtMallocPhysical 507899，halMemCreate drvRetCode=42，2MiB申请失败。
回溯npu_cudagraph_backend.py:103属于Graph前两次eager warmup，尚未进入torch.npu.graph；之后有507018/AICPU异常。
公开CANN驱动头文件中42为DRV_ERROR_PROCESS_EXIT；现场驱动版本的更早异常尚未取得，不能把where分配点认定为首发错误或普通HBM OOM。
用户要求先看原配置是否重现；剩余target/capacity/timeout/双rank/TP16共五段，一段一跑，中间由用户人工确认清理。
新增phase2 reference证据复用与target-only停止入口，008复用007，不重复已过组件/reference；失败非零，不自动推进。
仅修改四份验收脚本和两份CPU测试；无功能源码、SDK、kernel、通用shutdown改动。旧完整流程/容量续跑保留。
六项CPU检查通过：双侧target单段边界、完整输出恢复、真实git runner-only提交允许/功能改动拒绝、公共CLI/矩阵透传；ruff六文件、mypy两文件通过。
新增边界测试先在旧入口复现重复reference/未恢复输出；本地CPU不能证明507899已修复。26仍open，25不解锁。
命令见[ticket26-target-retry](../ticket26-target-retry.md)，证据见[target-only-resume.json](../verification/ticket26-local/target-only-resume.json)。

### 008 target再次失败：新增InplaceIndexAdd线索

用户回传32在15:43:33–34继续报507899/halMemCreate drvRetCode=42；Device7记录InplaceIndexAdd执行失败，随后报507018。
008只启动target，不能继续用相邻阶段快速切换解释；异步报错尚不足以确认根因。
新增单卡eager探针diagnose_index_add.py，逐调用同步真实PageObservation及runtime形状的五项index_add_计数。
两组输入各一次；Mac CPU共10项通过，ruff通过，Standards/Spec各0项未解决；NPU探针待用户执行。
未修改功能源码或清理行为，不重跑已过阶段、不推进后续。26仍open，25未解锁。
详见[008诊断证据与32命令](../ticket26-index-add-diagnosis.md)。

### 单卡计数通过，继续真实服务预热边界诊断

用户回传32探针十项全部PASS；这只覆盖单stream两组输入，不代表target已通过。
核对git历史：cache_updates/refill_verified/map_verified三处index_add_由791469d8e0新增，runtime另外两处为旧代码。
用户授权继续定位；新增默认关闭的NPU预热诊断，仅在eager warmup记录index_add_调用前后同步边界、位置、形状和索引。
独立脚本复用008有效配置，双机仅启动target一次到ready即退出，不发请求、不重跑已过测试、不改默认清理。
四项CPU检查通过；新增文件静态检查通过，既有graph backend九条历史lint保留；两轴评审各0未解决。
本轮NPU尚未运行，诊断成功也不是验收通过，26仍open。见[009诊断命令](../ticket26-startup-diagnosis.md)。

### 009诊断脚本解析失败，保留已有日志离线恢复

用户回传32 JSONDecodeError Extra data line1 column69，31 peer failed；尚无原始诊断片段。
本地两项新回归复现多rank完整JSON同一行导致旧解析失败，现逐marker raw_decode并显式保留损坏/failed记录。
新增--read-log离线入口，不启动服务、不改原summary/验收结果；必须32组有效warmup_complete且无错误才标证据完整。
六项CPU检查通过、ruff通过，两轴各0未解决。仅脚本/测试/文档改动，无NPU功能源码变化。
009原始日志待用户在验收容器读取，当前不能确认服务故障已消失；26保持open，不重跑已执行模型。


### 009同步预热证据完整，补齐跨stream存储使用登记

用户离线回传warmup_evidence_complete：16卡各两次、每次390个index_add_，无missing/failures/parse_errors。
这只证明诊断同步条件下12,480次调用完成，不代表异步路径或target验收通过，原失败报告保留。
代码审计发现主stream临时请求行号/索引在materialize返回后仍被refill/map使用；既有事件排序没有登记allocator跨stream使用。
仅NPU sparse manager的mempool miss/refill/map路径增加record_stream，保留并行与默认清理，不改kernel或额外扩大池。
该缺口是否为008根因尚未证明；下一轮010关闭诊断同步，只启动一次target到ready。
12项materialization、3项启动CPU检查通过；脚本/测试ruff通过，manager历史8条lint新增0条。
CPU生命周期测试模拟allocator保留，未复现NPU错误；新测试首轮为fixture失败，修正后通过，不作为驱动故障red/green证据。
Standards/Spec各0未解决。用户人工确认清理后执行[010命令](../ticket26-startup-diagnosis.md)，不重跑已过阶段；26仍open、25未解锁。


### 013：正常异步启动诊断通过，正式请求验收待执行

用户回传31成功标记及32完整summary：7358ebc26e、无tracked diff，index_diagnosis=false，diagnostic_startup_passed、cleanup=passed。
保持64/512、观测开启与原插件hash；正常异步预热及启动完成，本轮未复现007/008故障。
这支持生命周期修复方向，不能由单次成功确认唯一根因；未发生成请求，Graph replay与正式target仍not-run。
010因启动前显存占用在模型加载前失败；随后用户删除旧日志，011配置读取失败，未启动服务。
013从原始配置按既有configurations恢复到新目录，未重跑组件/reference。
旧007/008报告和reference输出已删除，正式resume当前缺少原始对照证据；不得伪造通过或自动重跑已过阶段。
下一步先确认有无备份；即使有备份，7358ebc26e含功能源码变化，复用仍须单独核对适用性，不能绕过身份检查。
26保持open、25未解锁；见[用户证据摘录](../verification/ticket26-local/async-013-user-result.json)。


### 用户授权重建一次reference，恢复正式target精度对照

旧报告无备份，用户明确允许只补一次reference；这是对“已过测试不重跑”的本次例外。
新增--reference-only运行原服务用例一次后停止；下一段--reference-service-report恢复响应并单独运行target。
组件测试不执行、报告标not-run；身份与原始响应检查保留，完整验收不标passed。
仅两份验收脚本及一份测试改动，功能源码与默认退出无变化；Mac首轮7项通过，评审修复后第二轮9项通过、ruff通过，Standards/Spec各0未解决。
014待用户双机运行，013启动通过不替代服务精度结果；26保持open。见[补跑命令](../ticket26-reference-rebuild.md)。


### 014预检配置矛盾：修正reference端口推导

用户启动即报by_segment requires shared prefix，无START launcher。service_ports私有配置关闭prefix_shared却保留by_segment，严格校验失败。
仅验收脚本派生配置设prefix_read_assignment=auto，原target配置不改；无功能源码/退出行为改动。
本地msgspec0.20 replace不执行post-init，初次测试未触发；显式校验原派生配置复现同一异常。
增强契约检查后，实际64/512两侧端口及原配置不变、残留19003阻断两项通过，ruff通过；Standards/Spec各0未解决。
015只重试尚未执行的reference，用户原授权不变；26仍open。见[新命令](../ticket26-reference-rebuild.md)。


### 015双侧reference通过，下一段016只跑target

用户回传31 REFERENCE_PASSED、32 passed_execution_scope/stopped_after=reference及paired清理passed。
组件未执行、完整验收incomplete；本轮只恢复了reference服务原始对照数据。
下一段使用同版配置及--reference-service-report严格读取015各侧目录，只执行target后停止。
用户确认资源清理后手动运行，不自动进入capacity/timeout；26仍open、25未解锁。
用户反馈摘录见[015结果](../verification/ticket26-local/reference-015-user-result.json)。


### 016 target长生成超过总时限，017显式延长至1800秒

用户回传前四HTTP200；第五8192+512在600秒被Router fail-stop。16rank均推进至315步，末步晚于Router超时。
直接失败为请求总时限，慢速原因未确认；原启动故障本轮未复现。整组target尚未通过。
新增仅reference-service-report适用的--service-timeout，记录600→1800，旧reference输出不改，其他身份严格核对。
六例共享prefix状态且最终联合校验，017需重建target目录并执行整组；组件/reference不重跑，进程清理由用户确认。
仅脚本/测试改动，Mac一轮11项、ruff通过，Standards/Spec各0未解决。见[017说明](../ticket26-target-timeout-retry.md)。
