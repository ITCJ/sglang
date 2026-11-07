# Ticket26：多基址后端实现交付

2026-11-06。本地实现与评审完成，**State 仍为 open，等待用户实机验收**。
本票没有开启25双写业务，也没有解锁25；kernel远端可见性与真实计算重叠仍待实测。

## 已实现行为

五类操作及保留兼容分支使用 binary-v1 多基址后端：主KV/Index K写回、swap-in、HBM回填、slot-map写入、prefix load_back gather。
单源通过同一后端适配。保留当前fill/admit的SDMA G2G batch/wait；load_back的collective和布局恢复不变。
生产与验证Python入口中的旧算子直接调用为零，检索记录见本票检查目录。

阶段1保留原调用结构；源码快照与62项定向CPU结果留存。
阶段2将当前HBM cache/current与P/D DRAM物化合成一次调用。
当前token仍由current HBM提供；回填仍等物化完成后读取selected HBM。
slot-map保持单独提交，attention返回前继续等待refill/map完成。

selected HBM按Graph batch形状共享给顺序执行的层，下一层复用前上一层的attention/refill/map已经完成。
Host兼容路径仍按调用分配临时selected buffer。
已捕获的copier及其九张metadata表由runtime保活；普通eager仅保留当前descriptor计划。
不会用record_stream代替未来replay的所有权。

## 表、容量与寻址

| 用途 | 当前固定集合 | 当前业务 / 预留 |
|---|---|---|
| 主KV物化 | 12源槽：P alive、D alive、8个prefix段、cache/current HBM | 当前业务用P/D/cache/current；25后11源形状由微测覆盖 |
| 主KV写 | 10目标槽：P alive、D alive、8个prefix段 | 只激活当前目标；每行预留两个任务 |
| P Index K写 | 9目标槽：P alive_index、8个prefix_index段 | 不增加D alive_index |
| 单源Host/HBM/slot-map | 一项源/目标表 | 使用同一kernel ABI |

默认部署为8个prefix段。较小拓扑的缺席槽保持invalid。
12是本票部署集合，不是kernel硬上限；底层容量来自tensor.numel，以uint64传入。
仍受内存和int64索引区间约束。旧交接最多8→8 eager、6→6二分Graph；本次新形状尚未实机验证。
报告必须区分容量、有效映射数和本次任务实际访问的基址数。

kernel ABI是两侧bases/ranges/base_valid，加src_index/dst_index/valid_mask，共九张固定设备表。
虚拟行地址为base+(index-range_begin)*row_bytes；区间有序且互不重叠。
主KV、Index K、4B slot-map按各自行宽分别调用；单行上限32768B。

原physical_page设备表保留。新增读位置表和按区域的写位置表，形状为[handle, logical_page, 2]。
字段为region、physical_page、valid；读表另有selected，选中副本失效时回到另一位置。
主KV区域号为0=P alive、1=D alive、2…9=prefix；HBM两路由cache/current任务单独选行。
Index K写表为0=P alive_index、1…8=prefix_index。
26安装lease时只激活正本；复用handle时同时恢复canonical region，不能残留旧区域。
写表第二位置已进入固定任务生成，更新内容即可激活第二目标，不新增kernel参数存储。
这不实现19复制策略，也不保证未来新增arena无需重新捕获。

沿用带层偏移的view base：物理行=page*layers*page_size+token_in_page。
特征分写再展开行内分片；不会重复加层偏移。
绑定/页表版本检查仍在forward/replay前，内容更新沿用binding event排序。

## 观测与验收入口

默认trace关闭。按源行数/字节仍表示有效复制任务载荷，不冒充总线流量。
融合后的分源时间为null并注明fused；每层materialize时间取copy_ready到全部物化分支完成的包络，作为暴露miss IO上界。
没有trace时T_step和设备时间均为null；不按字节分摊，不填0，不把上界再加进T_step。

`compare_multibase_observation.py`输出baseline/phase1/phase2同字段JSON和CSV。
旧日志从layer_pages恢复同一组源计数；新日志直接使用materialization记录。
当前比较脚本按GLM主KV 1152B/行解释旧日志。没有匹配的旧运行目录时，不伪造迁移前对照。

观测开启时，回填之后核对实际HBM内容；slot-map写完后再次查询新项及检查正条目数量。
两种核对使用独立设备缓冲，并在各自完成event前完成；报告只在现有forward完成后读取。
真实长生成必须出现evicted_entries、refilled_rows和slot_map_updates，且实际检查行数为正。
任意request/rank出现缓存内容、missing或stale错误，整体验证失败。

新微测复用14的双机协调、进程清理和归档方式：

- 4K/8K/32K/128K一层远端/双目标写，带与不带D本地读，先两rank，再16rank。
- TP16每对P rank按页写一份D副本，共8份；候选页全局每页一名写者。远端与候选mask分别生成。
- main远端、本机和Index K字节分列。共同完成event后，每个目标分别记completed或zero-work。
- 写者event完成并通知后，D立即从自己的映射读回HBM比对，不插入人为延迟。
- 双机12源/11活跃源、切页/切源、预留副本切换/失效、零有效行；一次capture后多次replay。
- D另捕获真实runtime空写图，然后注入过期版本，在replay入口之前验证fail-stop。
- 写微测也使用捕获后的Graph replay。无模型计算的pipeline只报告传输，不证明计算重叠。

14旧对照为SDK L2G batch/wait；旧16rank共同写D segment0的一份主KV。
新16rank是8份，不能直接用旧单份字节作分母。旧数字保存在[方案D节](ticket26-plan.md)。

## 实机运行

P/D各执行一条命令，使用同一个新run-id。需要已可运行的服务配置、模型和SDK。
`--sgl-source`指向服务器现有sgl-kernel-npu源码目录；只读取其CMake/helper，不改写安装库。
默认按CANN9.0.0与Ascend910_9382构建独立插件。不同构建配置先用build_multibase.py指定参数。

```bash
python ascend-mempool-test/scripts/run_ticket26.py \
  --role prefill \
  --config /path/to/prefill-config.json \
  --run-id k2-ticket26-001 \
  --sdk-build-id mf114-c01f3ad-ticket11-batchfix \
  --sgl-source /path/to/sgl-kernel-npu \
  --prefill-host <P地址> --decode-host <D地址>
```

D将role改为decode、config改为D配置，其余保持一致。
日志在`/home/cryang/log/k2/<run-id>`；完整服务回归沿既有入口保存在`/home/cryang/log/mempool-min/<run-id>-phase1/phase2`。
入口自动构建插件、运行两阶段单配置回归、检查真实淘汰、运行两种写者规模微测并清理自身子进程。
**用户缩减范围：两阶段均只跑 `(64,512)`，各一次；其余三组不跑。微测每档 samples=1、warmup=1、iterations=2，不自动重试。全部场景保留；小样本性能仅作粗测，不能代表稳定尾延迟。**

阶段1独立clone当前仓库后回到基点aa5852624e，再覆盖归档；不会把阶段2源码混入阶段1。
`--stage phase1|phase2|benchmark`可用于失败后的分阶段续跑，不删该阶段用例。
每阶段保留12/18写读、Graph和过期表测试，以及09的A/B/C/E、V1–V10完整ID和字节检查。

若SDK两端分别编译，可传`--sdk-source-confirmation`记录已经核对的源码一致性；不跳过二进制身份归档。

```bash
python ascend-mempool-test/scripts/compare_multibase_observation.py \
  --baseline /path/to/matched-baseline-observation \
  --phase1 /home/cryang/log/mempool-min/k2-ticket26-001-phase1 \
  --phase2 /home/cryang/log/mempool-min/k2-ticket26-001-phase2 \
  --output /home/cryang/log/k2/k2-ticket26-001/comparison
```

需要回传ticket26汇总、两阶段matrix/V1–V10、cache-eviction、每rank multibase-graph、带宽report/samples/background/environment以及comparison。
真实模型计算重叠尚无结果：入口始终明确unknown，25仍锁定；后续需同硬件同负载的并行计算/写入和尾部等待证据。

## 功能源码阅读顺序

均为专用代码；本票无通用主线功能源码改动。

| 文件（相对python/sglang/srt/hardware_backend/npu） | 用途 |
|---|---|
| mempool/multibase.py | 插件加载、固定基址/任务表、单源适配与捕获生命周期 |
| mempool/page_locations.py | 正本/副本候选与复用时的区域重置 |
| mempool/manager.py | 预分配读写位置表、lease原位安装和版本边界 |
| mempool/indexed_copy.py | 页先布局、特征分片与双位置写任务对接 |
| mempool/offload.py | 保留现有P/D/Index K写者，预建目标集合 |
| mempool/copy.py | 四来源物化、显式区域路由与已捕获copier保活 |
| mempool/decode_write.py | current HBM及保留的单源接口适配 |
| mempool/runtime.py | 物化接线、当前token排除、完成后观测归档 |
| sparsity_driven_kv_offload/manager.py | Host兼容路径、缓存回填/slot-map和写后核对 |
| sparsity_driven_kv_offload/attention.py | 稳定selected HBM的获取；既有等待/消费顺序保留 |
| mempool/prefix.py | load_back与pool_copy关闭时的保留分支适配 |
| mempool/observation_device.py | 设备源计数及缓存/slot-map实际检查 |
| mempool/kernels/unidex_multibase/host.cpp、kernel.cpp | 原样引入交接的独立binary-v1算子；身份另附 |

## 脚本、测试和文档

- 构建/编排：build_multibase.py、run_ticket26.py、run_bandwidth.py、bandwidth_worker.py。
- 设备工作负载：multibase_graph.py、multibase_write.py；bandwidth_device/report/summary适配新路径和分目标字节。
- 既有验证入口：verify_graph/fetch/page_first_copy/pool_sdma及sharing_copy/cd_copy均迁移；不改变SDMA被测路径。
- 观测分析：analyze_k2_observation.py、compare_multibase_observation.py。
- CPU oracle/测试：multibase_oracle.py、test_multibase.py、test_multibase_delivery.py，以及现有复制/资源/多层fetch测试的外部kernel替身适配。
- 当前文档、本票、方案及target-alignment D节同步；检查与阶段1快照在[verification/ticket26-local](verification/ticket26-local/)。

## 本地验证与证据边界

阶段1定向62项通过；阶段2完整套件运行一次，共598项。
其中24个方法受沙箱本地端口/进程权限影响，沙箱外只重跑这些方法，24项通过。
剩余6个既有失败方法涉及旧32位容量断言、端口预期和已删除preflight测试钩子，原始失败保留。
未为了本票修改无关服务行为来迎合旧测试。598项中592个方法经本轮运行通过。

新增补充检查验证预留第二写目标原位启用、8份远端+1份候选实际内容/字节、淘汰后的实际cache/map，以及任意rank错误不能被成功记录掩盖。
受评审修改影响的页表和trace定向检查通过；未重复执行完整套件。
本地相关22个源码模块严格类型检查通过，改动Python静态检查及归档源码编译通过。
Standards与Spec两轴评审的已报告问题均已修复，最终未解决各0项。

本机未编译CANN插件、未访问设备、未运行双机Graph/远端写/真实模型回归。
以上CPU和静态结果不替代实机验收；不勾选未实测验收项，不关闭26。

## 001 后续修复

31的page-first-copy验证arena因10.97 GiB申请超过8.65 GiB空闲而失败；32同检查通过。
测试arena改为8 slots、5.48 GiB，实际访问仍跨4 GiB；两阶段一致，生产布局不变。
新回归在修复前失败、修复后通过；实机结果待确认。重测使用新run-id `k2-ticket26-002`。

## 002 fetch捕获修复

首次gather可发生在Graph capture内；BaseTable改用设备empty+fill_初始化，消除Python列表到设备的同步拷贝。两阶段同步。
CPU检查通过，NPU重测待回传；先按[双机fetch重测](ticket26-fetch-retry.md)复用002插件，不重跑页拷贝或模型。

## 002重测通过后续跑

用户确认两侧fetch定向重测均ALL_CHECKS_PASSED。阶段1服务回归、阶段2、远端写仍未完成。
原命令改用 `--run-id k2-ticket26-003` 并增加 `--phase1-component-run-id k2-ticket26-002`，即可复用阶段1已通过组件报告。
本地会核对report的passed、rank和几何，归档原JSON与reused-from.json；缺失/失败报告不会跳过检查。
阶段2不复用阶段1组件结果。只跑64/512及两次微测测量的约定不变。

## 003 reference启动失败归因

用户回传的两份启动前npu-smi快照均显示已有SGLang scheduler每卡占约53,300 MiB，因此本次权重加载只剩约9 GiB并OOM。
后续31已空闲；两侧确认空闲后，原续跑命令改用 `--run-id k2-ticket26-004`，仍保留 `--phase1-component-run-id k2-ticket26-002`。
003没有服务验收通过结果。新增HBM缓冲的预算记账仍单独待核对。

## 005 capacity 失败后的续跑

005已完成reference/target，capacity因31的10000端口绑定冲突失败；runner漏查模型退出造成空等。
修复仅涉及验收脚本，功能代码和算子不变。两侧注册等待均检测非预期退出，预期容量拒绝仍走原验收契约。
使用新run-id 006与`--phase1-service-run-id k2-ticket26-005`，直接从capacity继续；不再重跑完成的组件/reference/target。
核对配置、模型、包版本与功能源码后复制原证据到新目录，写service-reused-from.json；不符直接失败，不自动重跑。
阶段1后继续阶段2和benchmark；两阶段各自的代码不同，未执行的阶段2仍须验证。
脚本：run_ticket26.py与run_k1_full_acceptance.py透传续跑参数，run_mempool_min.py处理阶段跳过及退出检测，mempool_service_resume.py核对/复制证据。
测试：test_service_resume.py覆盖跳过顺序、身份变化/缺失证据拒绝；test_mempool_full.py覆盖双侧启动失败与预期容量拒绝。
本地六项定向检查通过；评审修复后三项受影响检查通过；归档脚本一致且功能源码字节未变。
[两侧续跑命令](ticket26-capacity-resume.md)。完整实机验收仍未通过。

## 006阶段2reference失败后的续跑

阶段1执行已完成，阶段2copy/fetch通过。31 TP3绑定19003失败，导致reference启动退出及32握手连带失败；事后19003已空闲，历史占用者未知。
007使用`--start-from phase2 --phase2-component-run-id k2-ticket26-006`，跳过阶段1并复用阶段2组件报告，直接从reference继续。
新增逻辑仅在run_ticket26.py；test_ticket26_resume.py从公共CLI验证P/D各自不启动phase1、组件报告透传、继续两档rank数benchmark与报告范围。
两项CPU检查、ruff/mypy通过，不重跑完整CPU或已通过设备测试。功能源码、SDK及默认退出没有变化。
[31/32命令](ticket26-phase2-resume.md)，[失败证据](verification/ticket26-local/phase2-reference-port.json)。

## 007后的阶段清理审计

reference关闭共享，实际启动16个P/D会话；此前端口清理误用target共享布局，只验证单一BM store及共享NIC清单。
run_mempool_min.py现按阶段选择私有/共享端口；包含reference的预检检查两套端口，清理按刚退出的服务检查。
没有改变ticket10_processes的进程组清理、通用shutdown、SDK或kernel；保留现有TIME_WAIT有限等待，不用固定sleep冒充资源已释放。
test_mempool_session_acceptance.py新增真实Runner.stage调用回归，验证占用19003会阻止reference完成；连同三项已有端口/TIME_WAIT检查通过。
阶段1归档同步，功能源码字节未变。007最新32 target异常栈仍待回传，历史残留根因未证明。

## 008：只重试阶段2 target

007 reference已过；32 target在Graph前预热报drvRetCode=42/507899。运行前无NPU进程、Graph准备前约30GB空闲，不能据此认定旧任务残留或HBM不足。
用户改为逐段运行、人工确认清理。008使用--stage phase2 --phase2-reference-run-id k2-ticket26-007 --stop-after-target；只启动target，结束后不自动进入其它阶段。
新增功能源码：无。Ascend/NPU专用与通用主线均无改动。默认进程清理保持原样。
验收脚本：mempool_service_resume.py按reference/target边界复制已过证据，跨提交比较python树及未提交源码；run_mempool_min.py恢复原cases/responses/tokenizer并在target清理后退出；run_k1_full_acceptance.py验证部分报告；run_ticket26.py提供双侧公开入口。
测试：test_service_resume.py覆盖双侧单段顺序和真实git身份拒绝；test_ticket26_resume.py覆盖公开CLI、矩阵透传与部分状态。六项CPU通过，ruff/mypy通过。
phase1快照不变；此次只重试当前phase2。完整验收不标passed；剩余capacity/timeout/双rank/TP16待用户逐段确认。
[31/32单段命令](ticket26-target-retry.md)。

## 008失败后的单卡诊断

008 target仍在32失败，新增InplaceIndexAdd/AICPU设备记录；507899首因尚未确认。
本次仅新增脚本diagnose_index_add.py，复用PageObservation逐调用同步并断言计数；功能源码无改动。
CPU两组共10项检查通过，ruff通过，Standards/Spec各0未解决；NPU未运行。
[32单卡命令与证据范围](ticket26-index-add-diagnosis.md)。31无需启动，已过阶段保留，26仍open。

## 单卡通过后的预热诊断

用户确认32单卡输出十项PASS；真实服务多stream路径仍待定位。
新增NPU index_diagnosis.py及graph backend预热hook，默认关闭；独立diagnose_target_startup.py只启动一次双机target。
不修改通用主线、观测计数或shutdown。四项CPU检查通过，两轴评审各0未解决；NPU诊断待运行。
源码阅读清单、31/32命令与结果边界见[009预热诊断](ticket26-startup-diagnosis.md)。26保持open。

## 009日志解析修复

32报Extra data，31随peer失败；已本地复现完整JSON同一行拼接触发旧解析错误。
仅diagnose_target_startup.py/test_index_diagnosis.py改动，新增离线--read-log；不修改功能源码或原验收报告。
六项CPU与ruff通过，两轴各0未解决；实际009诊断片段尚待用户回传，只读取已有日志，不安排模型重跑。


## 009证据恢复与010异步启动候选修复

用户回传32组同步预热完整、每组390次index_add_，无错误；正式验收仍未通过。
本轮只改NPU sparsity_driven_kv_offload/manager.py：为主stream临时索引登记miss/refill/map消费者，防止消费完成前allocator复用存储。
通用主线源码无改动，无新增同步、缓冲池或shutdown改造；这仍是待NPU验证的候选根因。
脚本diagnose_target_startup.py新增--without-index-sync，单次target startup关闭诊断同步；两份CPU测试验证所有权登记和启动开关。
12+3项CPU通过，静态检查新增0问题，Standards/Spec各0未解决；硬件并发不能由CPU模拟证明。
[本轮源码清单、证据限制和31/32命令](ticket26-startup-diagnosis.md)。已过阶段不重跑，26仍open。


## 013通过后重建reference

013用户确认双侧正常异步启动及脚本清理通过，诊断同步关闭；尚无生成请求验收。
用户删除旧日志且无备份，明确允许只补一次reference，随后人工确认清理再单独target。
本轮功能源码无改动；两份验收脚本新增reference-only与service-only续跑，组件缺失明确not-run，保留严格身份核对。
首轮7项、评审修复后第二轮9项CPU与ruff通过，Standards/Spec各0未解决；[源码说明与014双机命令](ticket26-reference-rebuild.md)。


## 014预检修复

reference临时端口配置关闭共享时遗漏读者分配字段，导致by_segment校验失败。
验收脚本仅将临时prefix_read_assignment设auto；原target参数不变。严格校验复现，两项定向CPU通过。
功能源码无改动；015重试reference，见[ticket26-reference-rebuild](ticket26-reference-rebuild.md)。


## 016长生成总时限与017重试

015 reference用户已确认通过；016 target第5请求仍推进时触发600秒总时限。
新增显式--service-timeout 1800，只改变本次有效超时并记录reference差异，原精度对照及其它身份门禁保留。
功能源码无改动；两份脚本及一份测试，一轮11项CPU通过。017只重试target有状态六例，不重跑reference/组件。
[证据边界、源码说明与双侧命令](ticket26-target-timeout-retry.md)。


## 017 target通过，018独立容量拒绝检查

用户回传31/32 TARGET_PASSED，32 summary停止于target且成对清理passed。
这支持017服务验收完成；完整验收仍incomplete，缓存淘汰专项需离线读取现有JSONL。
新增--capacity-only，只执行既有capacity_stage与成对清理后停止；不重跑reference/target，不自动启动timeout。
Ascend/NPU及通用主线功能源码均无改动；仅run_mempool_min.py入口与test_service_resume.py检查。
新增单段入口CPU检查通过；现有成对容量拒绝检查首次受沙箱ps限制，获准沙箱外重试通过。首次选择旧测试类名有误，未执行测试体。
ruff通过，Standards/Spec各0未解决；硬件018待运行，26仍open、25未解锁。
见[018命令及证据边界](ticket26-capacity-only.md)。


## 018容量结果与019独立超时检查

用户回传31 CAPACITY_PASSED，经过成对容量证据、进程与端口清理屏障；32 summary未单独回传。
017缓存淘汰离线检查passed/evidence_entries=16，更新原结果记录；不重复已过服务。
新增--timeout-only，复用原stage(target=True,fault_only=True)，仅故障注入及双侧清理后停止。
原600秒operation_timeout不改；功能源码/设备算子/通用退出路径不改。
两项CPU检查一轮通过，ruff通过，Standards/Spec各0未解决；019硬件验收待执行。
脚本和测试清单、双侧命令见[019说明](ticket26-timeout-only.md)。26仍open，25未解锁。


## 019超时通过标记与020两rank微测

用户回传31 TIMEOUT_PASSED，原路径已核验D故障日志、退出码及双侧清理；32 summary未单独回传。
下一段直接调用既有run_bandwidth.py，multibase/remote-write、设备0/1、NUMA0、samples1/warmup1/iterations2。
不使用run_ticket26 --stage benchmark，避免自动接着启动TP16；不重复已过服务或组件。
双侧CLI计划检查确认22档、同配置；未运行NPU。仅更新说明与用户结果，功能源码/脚本/测试无修改。
模型计算重叠仍未知，迁移观测同负载对照尚未归档；两档微测也不能单独宣称整个26通过或解锁25。
见[020双机命令](ticket26-pair-benchmark.md)。26仍open。


## 020微测batch契约修复与021重试

用户补回019 decode TIMEOUT_PASSED；020双侧失败，D真实runtime.write_layer访问缺失的extend_seq_lens抛AttributeError。
微测SimpleNamespace未提供四个正式ForwardBatch默认可选字段；提取同一个batch构造函数用于CPU复现，补None。
一项定向CPU测试先红（同一AttributeError）后绿；真实runtime空写不改KV/计数，过期页表仍在replay前拒绝。
生产Ascend/NPU与通用主线源码无改动；仅测试支持multibase_graph.py及test_multibase_delivery.py。
22档带宽尚未启动；前置GVA日志单独保留，不能据此认定SDK根因或已修复。
021复用插件，仅重试两rank微测，命令见[微测说明](ticket26-pair-benchmark.md)。26仍open，TP16不自动启动。

本轮ruff通过；Standards与Spec评审各0项未解决。


## 021双侧微测通过与022 TP16

用户回传双侧完成标记、passed_measurement_scope、cleanup passed及四份Graph通过报告。
source_capacity=12但post25_active_sources=4/full_shape=false，只覆盖一个NUMA组；完整形状待TP16。
128K remote43.163GB/s、dual80.137GB/s按各自物理字节计，不能直接判断速度倍数；每档两次，仅粗测。
D读p99基线1.163ms，128K remote干扰2.479ms、dual干扰2.994ms。物理落点和计算重叠未证明。
022只运行TP16微测，原22档与Graph检查保留；真实CLI双侧计划核对通过，未运行硬件。
本轮仅文档与证据记录，无功能源码、脚本或测试改动。26仍open、25未解锁。
[022命令](ticket26-tp16-benchmark.md)。


## 022 TP16通过及性能归档

用户回传31完成标记、prefill report/runner和32份本地/peer Graph汇总；32终端完成标记未单独回传。
报告passed_measurement_scope、cleanup passed；32份Graph均passed，容量12、活跃源11、full_shape=true，无缺失。
128K远端写287.839GB/s、双写314.699GB/s；相对021分别6.67倍、3.93倍。
D读基线6.037ms；128K远端写干扰12.626ms（2.092倍），双写9.001ms（1.491倍）。
本轮仅归档用户硬件证据和文档，无功能源码、脚本、测试改动或硬件重跑。
迁移同负载观测对照仍待整理，模型计算重叠未知；物理NUMA落点未验证。
26保持open，25未解锁。见ticket26-performance-021-022.md及verification/ticket26-local/tp16-022-user-result.json。


## 023：017离线观测清点

用户回传16份JSONL，共732,576条materialization及同数量layer_pages。
每rank45,786条，合计9,392个forward记录×78层；source_rows字段缺失为0。
96条request_tracked是6请求×16rank，不能解释为96个独立请求。
来源计数：P_alive 280,470,816，D_alive 3,565,280，HBM_cache 1,206,643,520，HBM_current 725,312；prefix八槽均0。
这是跨请求、rank、层、step累计的有效任务行数，不是去重KV容量或硬件事务流量。

当前26不启用25的直接prefix读路由，017的prefix槽为0不否定已有prefix load_back/复用行为。
022专项已单独验证未来11源形状；不能把专项形状当作017服务实际使用形状。
runtime.collect_observation将current_dram_rows/refill_dram_bytes直接写0；它们不是独立设备测量，不以字段为0单独证明无DRAM重读。
没有trace_view.json，设备物化耗时及计算重叠保持未知；host事件不能替代设备窗口。
baseline/phase1原始观测已删除，现存phase2计数无法证明迁移前后一致。

已提出每版本一个同负载短请求的补采方案，等待用户确认是否允许三版本短采样；不自动重跑完整验收。
本轮只整理证据、阅读代码，不修改功能源码或测试、不访问NPU。
26保持open，25未解锁。证据：verification/ticket26-local/observation-023-user-result.json。
