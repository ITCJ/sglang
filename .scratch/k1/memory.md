# K1 项目记忆

更新：2026-11-02；设计决定日期：2026-11-01。12已由用户确认004验收关闭；11已解锁、尚未实施。
11本轮仅完成方案草案：建议先由high实施独立SDMA demo，用户运行后再定稿逐op路径并核对，之后才授权工程代码。
先读[demo交接](ticket11-demo-handoff.md)；[工程草案](ticket11-implementation-plan.md)仍待结果和用户确认。
SDK按用户指定使用本地GitCode `../gitcode/memfabric_hybrid`（1.3.0，29fbd1ae14e7），不以较旧GitHub版本判断实现。
本地batch子提交错误传播存在待核对缺口；demo未编写、NPU未运行，11仍open、09仍阻塞。
最新硬件反馈：31/32的c2ed808092、k1-ticket12-004自动验收均passed，page-first-copy/fetch及两轮六例完成，五类布局/签名和字节核对通过。见[004核对记录与边界](verification/k1-ticket12-004/README.md)。用户已明确回复“关闭”；12已closed，11已解锁但未实施。
这是会话恢复摘要；逐票事实以最新 Comments 和用户验收记录为准，实施细节以执行文本为准。

## 11 demo 最新交付（2026-11-02）

用户已授权implement demo；共用pool_copy适配层、manager装配口、两组P/D设备校验及自运行入口已实现。
路径已定，demo后仅核对接口事实；不做八组demo，服务不新增目标读回。
CPU完整一次547项；21个沙箱受限方法补跑通过；1项既有test_config的旧UINT32_MAX预期失败保留。
本次新增测试、静态及适配层strict mypy通过，双轴复核无阻断；NPU未运行。
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

- K1共12票：01、02、03、05、06、07、08、12已closed；04、10、11、09为open（8 closed、4 open）。
- 主线为07 → 08 → **12 → 11 → 09**；04和10不阻塞。12直接依赖08，11直接依赖12，09直接依赖06、08、11并传递依赖12。
- 08已由用户验收；12代码与验收入口已交付，CPU合并541项中540项通过；1项旧注册夹具修正后按两轮限制未第三次运行，不计为通过。自身NPU004已由用户确认验收，详见[交付说明](delivery-ticket12.md)。12已关闭，11已解锁、尚未实施。
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
fill/load_back有独立设备流，仍由worker有序路径提交；admit在调用流提交，三者查询设备事件完成。
已有独立设备流不代表票11的独立主机执行器线程已经实现。

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
