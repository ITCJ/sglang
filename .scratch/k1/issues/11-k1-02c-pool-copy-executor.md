# 11: K1-02c admit 与 fill 的池间搬运路径与分片执行器（行为不变）

**What to build:** 在票 08 定稿的 8 / 8 / 1 / 1 / 1 副本布局与票 12 的 page first 排列上（2026-11-01），把 op1 fill 与 op3 admit 这两条 DRAM 到 DRAM 的池间搬运（主 KV 与 Index K；所有主存池间的连续块）统一改成按搬运清单（每页每类一块：主 KV 11,501,568 B、Index K 688,128 B，用票 12 的页块接口取地址，两类同一张清单）走内存池库的批量异步接口；执行器等待或查询库的真实完成后才回报“目标可读”，设备事件不算（完成桥接）；fill 的完成进 prompt ready 汇总，admit 的完成才 publish；每个 SR 分片一个独立执行器线程与队列（绑本 NUMA、整张清单一次提交、拷贝期间释放 GIL），fill 现有的独立队列并入它；协调者在有序调度 tick 里下发任务、保护源、分配目标页，汇总完成、收齐该 op 全部参与分片的“目标可读”再向 main router ack；worker 只报源写完。调度线程与 SR 控制循环不等搬运。07、08 已把选页、地址解析、拷贝提交分开，本票只换底层的提交与完成回报。不碰 op2、P 的 write_through、D 的逐步写回、P 到 D 的 Index K 传输（模型工作内存与池之间的写回、恢复及动态索引回填继续独立处理）。副本数（8 / 8 / 1 / 1 / 1）、页归属、分片分工、串行、ack 语义、保护 / 发布 / 释放语义、main router 消息都不变。细节见 k1-tickets.md 第 15 节。

**Blocked by:** [12（K1-05d 主存五类区域改 page first（布局正确性，行为不变））](12-k1-05d-page-first-layout.md)（12已由用户确认004验收并关闭，前置满足）；阻塞09。

**Status:** ready-for-agent

**State:** open

- [ ] A / B / C / E 与 V1–V10 全过；V2 加 op1 后 alive、op3 后 prefix 与源逐页一致（主 KV 与 Index K 都比）
- [ ] op1 / op3 期间计算侧拷贝算子零调用；清单按物理目标分别核：admit 全部分片页块条数之和 = 2N、字节 = N × (M + I)；fill = 9N（8 份主 KV + 1 份 Index K）、字节 = N × (8M + I)；合并后条数与覆盖范围另记
- [ ] 搬运期间调度 tick 间隔与空闲时一致，SR 仍处理消息
- [ ] admit：提交 < 库完成（目标可读） < ack < publish < 释放源 < 归还额度；fill：提交 < 目标可读 < 进入 prompt ready 汇总（结构化日志；设备事件与库完成分开记）
- [ ] 负测试：注入延迟后 ack、publish、释放同步推迟不提前；fail-stop 下执行器线程与共享会话关闭干净
- [ ] 票 08 的证据项（读字节计数、admit 每页单写者、写全部落本段）在新路径上复核不变
- [ ] 与票 12 验收后的状态对照（不重跑 08、12；没有可并列的性能表时只记两段时间与 n），最多 2 轮，重点 op1、op3（提交到完成两段）、T_tail、T_free
- [ ] 单 commit；Standards / Spec 两轴评审

## Comments

### 2026-11-01：导入同步稿

2026-10-31加票并改序到07/08之后；从08验收后的状态开工，阻塞09。尚未实现。

### 2026-11-01：08用户验收关闭，解除前置阻塞

用户明确接受08的001两轮精简NPU验收并确认关闭。08已closed，本票已解锁，Status=ready-for-agent、State=open；尚未开始实现。
后续顺序11→09；08保留的证据边界见[验收确认](../verification/k1-ticket08-001/README.md)。

### 2026-11-02：同步page first改序定稿

同步新版第15节：直接依赖由08改为12，先等12实现并经用户确认NPU验收关闭。按页块计数（admit 2N、fill 9N），使用12的页块接口，分别核对库完成、设备事件和admit/fill完成链。旧“08关闭即解锁11”记录保留为历史。本票未实施，State=open。

### 2026-11-02：12用户验收关闭，本票解锁

用户确认c2ed808092的k1-ticket12-004精简功能验收并关闭12。本票ready-for-agent/open，尚未实施；后续对照基线为12已接受的page-first状态，09继续等待本票。

### 2026-11-02：仅规划，建议先验证SDMA再定稿工程路径

用户要求规划agent只写方案，后续交high实施；提出先做独立demo，再核对各op的源、目标与执行NPU，最后开始工程代码。
已编写[demo交接](../ticket11-demo-handoff.md)、[工程接入草案](../ticket11-implementation-plan.md)与[路径核对页](../ticket11-path-review.md)。
按用户指定，SDK依据是本地GitCode 1.3.0/29fbd1ae14e7；已核对普通SDMA、线程stream、GIL及batch子提交错误传播缺口。
建议路线、固定一线程及服务验收目标读回仍待核对；没有修改正式验收范围，没有实现demo/工程或运行NPU。
State保持open，09仍阻塞；必须带回demo证据并经用户确认路径后才能进入工程实施。

### 2026-11-02：demo实施交付，服务尚未接入

按用户implement授权完成共用平台适配层及两组P/D demo。执行器submit/poll、同线程BM batch/wait、UNKNOWN锁存与父进程清理已接线。
设备索引读回覆盖admit prefix和D读取fill后的P alive；8页真实几何，接口事实与functional smoke分开报告。
完整CPU一次547项，21个沙箱受限方法补跑通过；1项旧test_config UINT32_MAX预期失败保留，未改无关代码。
新测试、静态与pool_copy strict mypy通过；Standards/Spec复核均无剩余阻断。
[运行说明](../../../ascend-mempool-test/K1_TICKET11_DEMO.md)和[验证记录](../verification/ticket11-demo-cpu/README.md)已交付。
NPU未运行，不修改SDK、不接服务fill/admit；11保持open、09仍阻塞。路径不重复确认，demo后核对库接口事实与证据。

### 2026-11-02：demo001双方launcher成功，待逐rank证据

用户回传31/32 launcher均exit_code=0、children_reaped=true，实际端口重绑全部passed。
按746ec2d54c入口判据可推断功能smoke完成；实际commit、传输及GIL细节待已有all-ranks.json核实。
[反馈记录](../verification/k1-ticket11-demo-001/README.md)已归档。interface_ready=false为固定保留标记，不是本次实测失败。
本次只归档，不重跑、不开始服务接入；11保持open，09阻塞。

### 2026-11-02：demo001逐rank功能与控制推进证据通过

用户回传8rank摘要，全部746ec2d54c且passed。admit16块/97,517,568B，fill24块/189,530,112B；实际submit/wait均0。
admit两个owner wait期间各10tick；fill四rank分别15/12/11/13tick；D零搬运为预期，设备读者校验通过。
[摘要与边界](../verification/k1-ticket11-demo-001/README.md)已补齐。mf_version未知，实际SDK构建、子任务错误传播和可见性契约仍待核对。
本次仅归档，无重跑、无生产接入；11仍open、09阻塞。

### 2026-11-02：两侧SDK版本确认及1.1.4源码核对

用户确认memfabric_hybrid=1.1.4、memfabric_zbal=1.1.2。本地GitCode v1.1.4/c01f3ad842b9可用，普通batch也存在子提交错误丢失路径。
partial_succeed大块走库线程池，调用线程wait不自动覆盖子线程流，不能直接替换。
[001记录](../verification/k1-ticket11-demo-001/README.md)补充tag和证据限制；未修改SDK、未重跑、未接生产，11仍open。

### 2026-11-02：工程服务接入实现，等待修复SDK上的NPU验收

用户明确授权开始工程部分。基于746ec2d54c，P各分片fill/admit接入共用PoolCopyExecutor，
按完整prompt分工生成混合主KV/Index K页块清单；tick保留状态/汇总，库真实完成才receipt。
空清单、UNKNOWN锁存、源释放门与父进程清理保留；load_back未切换，通用主线源码改动0。
配置pool_copy默认false；需先部署GitCode v1.1.4最小batch错误传播补丁再开启。
补丁仅作为交付文件，未改外部SDK或安装远端。build-id仅为审计标签，不能证明二进制已修复。
[工程交付](../../../ascend-mempool-test/K1_TICKET11.md)列出功能阅读顺序、SDK构建前提与31/32命令。
两轮服务入口复用既有准备/请求/清理；强制提交前50ms延迟、16分片、2N/9N/字节与submit→库完成→receipt顺序。
不新增服务目标读回，按用户已确认的demo独立设备比较 + 服务B/C/V2/字节证据边界交付。
[CPU记录](../verification/ticket11-service-cpu/README.md)：完整548项一次，沙箱受限21方法补跑通过；
后增检查器1项通过，合计549项中548通过，保留旧UINT32_MAX预期1项失败。静态/类型检查通过。
Standards无阻断；Spec提出的检查器覆盖和顺序/延迟缺口已修复。
SDK函数原文提取体补丁红绿验证通过；完整SDK构建、真实在途失败/超时及服务NPU验收仍未执行。
不把提交前延迟当作硬件在途注入或性能验证；11保持open，09仍阻塞。
