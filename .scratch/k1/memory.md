# K1 项目记忆

最新09反馈002：128/512两侧passed，64/128在target报JSONDecodeError(char4964)，64/512未运行。
连同001的128/128，现有两组128页已报告通过；09仍open。
用户取证已确认TP长JSON交错；修复为独立prefix-content文件+短日志引用，保留完整校验。
11项CPU与8模块Linux目标类型检查通过；003从b64-sd128继续，128两组不重跑。
回传须保留prefix-content目录；见verification/k1-ticket09-002/README.md。

最新09反馈：001的128/128两侧passed；128/512在D:10000端口preflight失败，64两组not-run。
测试入口现将10000纳入释放检查，preflight复用TIME_WAIT等待；真实监听仍失败。
002用--start-from b128-sd512续跑，保留001首组证据。09未关闭，详见verification/k1-ticket09-001/README.md。

更新：2026-11-02。用户确认31/32的11工程002验收并明确关闭；11已closed，09已解锁、尚未实施。
用户随后要求09保持完整验收并覆盖B=128/64、S_D=128/512四组。
[09实施计划](ticket09-implementation-plan.md)已写；prefix维持5120 tokens，四组均not-run。
生产RouterConfig目前也限制B=128与S_D=128；D哨兵已清理，计划按验收开关恢复。
09现已按计划接通四组入口与D验收证据，CPU关键回归通过，既有UINT32_MAX旧预期失败保留。
运行说明见../../ascend-mempool-test/K1_TICKET09.md；四组NPU待用户运行，09保持open。
用户回传demo-001的31/32 launcher：两侧exit_code=0、children_reaped=true，实际端口均passed。
8rank摘要已核实746ec2d54c：admit16块/97,517,568B，fill24块/189,530,112B；设备内容通过，实际wait期间P控制循环均推进。
用户确认双方memfabric_hybrid=1.1.4、memfabric_zbal=1.1.2；原unknown只是模块版本属性未取得。
本地GitCode v1.1.4/c01f3ad842b9也有普通batch子提交错误丢失缺口；partial_succeed涉及其它线程流，不可直接替换。
部署二进制与tag一致性仍有限制；demo功能结论保留，不重跑、不自动升级SDK。
[001反馈](verification/k1-ticket11-demo-001/README.md)保留证据边界；interface_ready=false不是本次测试失败判据。
11已closed、09已解锁。工程部署依据为GitCode v1.1.4/c01f3ad842b9加最小错误传播补丁；硬件可见性证据边界保留。

## 11 工程002：用户确认关闭（2026-11-02）

用户回传status=passed，target/restart每轮4操作、64分片记录。
P基础验收与额外搬运检查通过；001的setup OSError22在本次未再出现。
指导修复版本bad113345e，摘要无commit字段；SDK标签mf114-c01f3ad-ticket11-batchfix。
[002记录](verification/k1-ticket11-002/README.md)保留原始JSON和证据边界。
32/D最终报告也passed，SDK标签与P一致，双侧结果已收齐。
用户明确回复“关闭”；11已closed，09已解锁。保留未验证项，不重跑。

## 11 工程001失败与修复（2026-11-02历史记录）

用户确认31/32 Docker已安装SDK补丁。001首请求D finished后，P执行器setup异常导致fail-stop/HTTP500。
旧CPU绑核将目标NUMA与继承scheduler mask求交，可空集EINVAL；公共Executor回归准确复现同形错误。
修复直接请求目标NUMA CPU，由Linux cpuset过滤；setup日志保留原始栈，不静默关闭绑核。
本地新增回归先红后绿，适配层3项通过，ruff/strict mypy通过；未运行NPU。
现场原始mask/栈未取得，仍需002确认是否仅此原因；沿用已安装SDK，不重编。
[失败与修复记录](verification/ticket11-affinity-fix/README.md)。11仍open、09阻塞。

## 11 工程交付时记录（2026-11-02，后续已由002验收关闭）

用户明确授权按此前计划实现工程部分。P各分片fill/admit已接共用池库执行器，tick submit/poll；
空清单仍汇总、完整prompt分工、正常释放后retire；load_back与原有父进程清理不变。
新增pool_copy显式开关，默认false；部署修复SDK后开启。GitCode v1.1.4普通batch子提交错误丢失以独立单行补丁交付，
未自动安装或修改外部SDK。构建标识仅审计，不证明加载修复库。
两轮自运行服务验收复用原入口，强制50ms提交前延迟、16分片、2N/9N/字节及完成到receipt顺序；服务不新增目标读回。
CPU合并549项中548通过、1项既有UINT32_MAX用例失败；Standards/Spec代码复核问题已修复。
真实SDK在途故障/超时、服务NPU与性能尚未验证，保留为验收缺口，不能用CPU替代。
[工程运行说明](../../ascend-mempool-test/K1_TICKET11.md)、[CPU证据](verification/ticket11-service-cpu/README.md)。
11仍open、09阻塞，等待用户NPU验收。

## 11 demo 交付时记录（2026-11-02历史记录）

用户已授权implement demo；共用pool_copy适配层、manager装配口、两组P/D设备校验及自运行入口已实现。
路径已定，demo后仅核对接口事实；不做八组demo，服务不新增目标读回。
CPU完整一次547项；21个沙箱受限方法补跑通过；1项既有test_config的旧UINT32_MAX预期失败保留。
本次新增测试、静态及适配层strict mypy通过，双轴复核无阻断；交付时NPU未运行，最新用户结果见上方001记录。
命令和证据边界见[demo交付](../../ascend-mempool-test/K1_TICKET11_DEMO.md)。11仍open，09阻塞。

## 恢复顺序与依据

1. 读[规格索引](spec.md)和待办票的最新 Comments，核对当前分支、工作区及直接依赖。
2. 读[K1执行文本](k1-tickets.md)对应章节：12在第16节，11在第15节，09在第13节。
3. 12实施前读[2026-11-01现状与建议稿](ticket12-design-handoff.md)及[08用户验收](verification/k1-ticket08-001/README.md)。建议稿是事实基线，方案已由第16节定稿。
4. 回查决策时读[本地M17/M58摘录](../mempool-min/discussion.md)；完整设计源在[syncfile](/Users/hibikid/Documents/syncfile/sglang-kv-explainer/mempool-min-demo/discussion.md)。

设计agent负责整体架构逻辑，不掌握Ascend/NPU具体实现。实际API、内核、设备地址、
stream/event与图捕获由实施侧核对本仓库源码及实验后决定；设计建议不等于硬件能力已验证。
执行文本的模块列表是影响分析，不要求逐文件都修改。需要改变已确认行为边界时再与用户讨论。

## 当前进度与依赖

- K1共12票：01、02、03、05、06、07、08、11、12已closed；04、10、09为open（9 closed、3 open）。
- 主线为07 → 08 → **12 → 11 → 09**；04和10不阻塞。12直接依赖08，11直接依赖12，09直接依赖06、08、11并传递依赖12。
- 08已由用户验收；12代码与验收入口已交付，CPU合并541项中540项通过；1项旧注册夹具修正后按两轮限制未第三次运行，不计为通过。自身NPU004已由用户确认验收，详见[交付说明](delivery-ticket12.md)。12已关闭，11工程002也已由用户确认关闭；下一票09已解锁。
- 旧“08关闭后11已解锁”是历史状态。旧阶段0的02只待单独关闭确认，不是K1票、不阻塞本链。
- 功能基线为08实现提交`4441eb9015`；关闭记录提交`b4a925a3a3`。后续读实际git状态，保留用户未提交修改。

## 08之后已经成立的事实

五类主存区域的副本数为`8 / 8 / 1 / 1 / 1`：P alive主KV、D alive主KV、
P alive Index K、prefix主KV、prefix Index K。D生成的Index K仍只在设备内存。
底层会话为P/D共32参与者；物理副本、映射者、写者、会话参与者分别计数。
设备映射覆盖实际访问者：fill分片访问所有prefix段，admit owner访问本组alive和NUMA0的Index K。

08验收基线是layer-first；12已改page first，004已由用户确认验收。alive仍整槽预留；prefix有40页、8段各5页，
描述符页号为`segment × 5 + local_page`；按请求轮转起始段，段满取剩余页最多者。
P/D各16槽，验收配置`S_P=8192`、`S_D=128`、统一`B=128`，仍单请求串行。
旧路径fill/load_back有独立设备流，admit在调用流提交，查询设备事件完成。
票11新路径开启pool_copy后，fill/admit由独立主机线程执行BM batch/wait；load_back仍用原事件。

## 12的固定边界与第一步

五类区域一次改为`[physical_page, layer, token_in_page, head, feature]`。
C32是设计决策编号；page first改变主存排列，HBM原生格式保持不变。
主KV与Index K独立分区，同一逻辑页分别对应连续块；几何从模型推导。

```text
page_bytes = L × B × row_bytes
alive physical_page = slot × (S / B) + page_in_slot
prefix physical_page = local_page（区域为所在段）
address = region_base + physical_page × page_bytes
          + layer × B × row_bytes + token_in_page × row_bytes + row_offset
```

当前GLM-5.2：主KV的`L=78, row_bytes=1152`，每页`M=11,501,568 B`；
Index K的`L=21, row_bytes=256`，每页`I=688,128 B`。
只保留page first，不设生产开关；布局类型、B、各维度进入注册/describe及启动签名。
重启重建池；页归属、写者分工、prefix分配/描述符、整槽后端、控制协议与完成语义继续沿用。

**第一步先验证拷贝接口并归档证据，再改布局。** 现有接口有单行32 KiB、
跨度`UINT32_MAX`限制；完整P主KV区域约10.97 GiB。用小几何及真实P几何验证，
默认alive按槽、prefix按段给基址（约702 MiB/55 MiB），保持单次跨度小于4 GiB。
验证失败才最小扩展为64位偏移。本机源码契约和CPU地址核对已通过，002已报告独立NPU copy/Graph步骤通过，端到端仍未通过；见[首份证据](verification/ticket12-copy-contract/README.md)。

后续顺序：布局 → 会话/视图 → 写回/稀疏读 → prefix三操作 → 注册/签名。
token位置到物理行的变换在设备上更新稳定索引缓冲并进入图捕获，不增加每步主机读回或同步。
12仍使用逐层索引拷贝；load_back仍逐层读、两块汇总缓冲、原生散开。
页块接口返回`区域 + 物理页 → 池库地址、设备地址、page_bytes`；两类地址保持区分。
12不引入11的执行器与批量接口，也不恢复性能埋点。

## 11接入时的口径

| 操作 | N页的页块数 | 有效字节 | 32页块数 | 完成链 |
|---|---:|---|---:|---|
| admit | 2N | N × (M + I) | 64 | 提交 → 库完成/目标可读 → ack → publish → 释放源 → 归还额度 |
| fill | 9N | N × (8M + I) | 288 | 提交 → 库完成/目标可读 → prompt ready汇总；无publish |

按物理目标副本展开，合并后的条数另计。旧`N × 99`只适用于layer-first的admit。
11须验证库完成与设备事件之间的完成桥接；旧设备事件不能证明库传输队列已完成。
对照状态为12验收后，08/12没有可并列性能表，不为此重跑。

## 验收边界

- 沿用08批准的源快照、正常load_back后设备数据、写读记录；不新增直接主存读回。物理NUMA驻留、性能、直接主存比较不在12范围。
- 12的CPU证据须覆盖独立oracle（写读双方不共用待测公式，fake copy随契约更新）、宽地址、跨页/槽/段、页块与行公式一致。
- NPU验证覆盖写回/稀疏读、prefix 31/32页、零页读者、换槽图重放、布局签名；复用08两轮六例入口，字节计数不变。
- B的主KV load_back为368,050,176 B，fill为2,944,401,408 B；Index K各22,020,096 B。C的31页在各段不均匀是正常现象，通信padding不算主存读取。
- 07严格答案报告仍failed；用户接受源/恢复校验及同输入冷热26 tokens一致的边界。08的V3为独立逻辑答案、V8为真实注册的CPU重放；不能扩大为直接主存或物理NUMA验证通过。
- 实机由用户运行；最多2轮并复用服务启动，双机每机一条自运行命令，日志到`/home/cryang/log/`，失败非零退出。CPU通过不替代NPU验收。
- 12单commit、Standards/Spec两轴评审及交付说明；沿用[阶段验收流程](../ascend-mempool/verification.md)和默认父进程清理。通用主线功能改动须在实施前说明涉及文件、必要性与行为影响。
- 每个实现步骤更新[目标页D](../mempool-min/target-alignment.md)并重生成HTML，分别记录代码、CPU和用户确认的NPU状态。
