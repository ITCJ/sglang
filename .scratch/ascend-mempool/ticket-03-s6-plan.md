# Ticket03 S6：全量 review、shadow 删除与代码清理

日期：2026-10-05，进度更新：2026-10-06。需求来源：[ticket03](issues/03-prefill-direct-offload.md)及用户确认的
S5/S6调整。S6.1首次全量review已完成，整改和后续步骤待实施；Ticket03保持open。

**Blocked by:** 无阶段阻塞。[S5](ticket-03-s5-plan.md)已于2026-10-06在当前已验证容量范围
通过用户验收；长上下文容量由用户独立处理。S6已进入审查/整改阶段。

## 交付目标

对01–03开发引入的全部mempool实现和共享接入点完成code review及整改，删除全部
shadow专用代码，清理冗余检查、重复逻辑和命名问题。最终代码保留正式mempool模式
及原普通sparse PD模式，并在清理后的同一版本重新通过S5和普通模式回归。

这次review覆盖整个mempool开发增量，不能只检查S6最后几个修改。S5发现的功能或
性能问题应在S5解决；S6负责在已经验收的链路上进行全面审查、清理及复验。

## A. 固定范围并完成全量 review

实施开始时记录review起点和目标commit；起点应覆盖首次mempool改动之前，结合
01–03交付记录核对变更集合。若存在未提交修改，一并明确纳入的diff。项目历史基线
与术语参照[CONTEXT](../../CONTEXT.md)，不混淆项目基线与本次mempool review起点。

审查范围包括配置/资源、BM布局与映射、writer/fetch/runtime、sparse cache、Graph、
PD传输与控制、stream/drain、共享生命周期hooks，以及相应测试、脚本和当前文档。

| 审查方向 | 需要回答的问题 |
| --- | --- |
| 仓库规范 | 是否符合项目约定，修改是否局限于必要入口，普通路径是否仍可独立运行？ |
| Spec与阶段要求 | 正式取数、零旧存储/流量、Index K handoff、Graph和释放合同是否完整满足？ |
| 职责和接口 | storage、runtime、cache、transport和control是否各自拥有明确职责，有无重复状态或越层耦合？ |
| 可读性与命名 | 能否直接区分request row、P/D slot、generation、token position和真实完成状态？ |
| 检查与热路径 | 每项检查保护什么不变量，放置位置是否正确，是否存在重复同步、转换或逐token静态检查？ |
| 测试与证据 | 测试是否验证真实行为，是否存在仅断言实现细节或依赖已删除shadow的空洞覆盖？ |

输出可追踪的review记录：发现、影响、位置、整改方式、验证和复查结论。按“仓库规范”
和“需求符合性”分别给出结果，并纳入可读性与设计整改。影响本票验收的问题解决后
再进入最终验收，不能只写一句“review通过”。

## B. 删除全部 shadow 专用代码

| 对象 | 最终处理 |
| --- | --- |
| P/D shadow运行模式及选择分支 | 删除；mempool启用选择正式模式，关闭恢复原普通模式 |
| 以旧host为参考的双路径readback/对照 | 删除专用实现、调用、状态、配置、日志与统计 |
| shadow专用环境设置和启动参数 | 删除定义与调用，更新脚本；不新增隐藏shadow入口 |
| shadow专用脚本、fixtures和测试代码 | 删除；仍有价值的公共行为覆盖先迁移到正式/普通入口 |
| BM writer、binding、Graph hooks、drain | 按正式路径的实际需要保留并精简，不能随shadow调用点一起误删 |
| 独立已知pattern的BM writer/copy验证 | 保留独立数据依据，迁移脱离shadow后继续验证正式取数 |
| 原非mempool的KV传输、host/staging链路 | 保留；关闭mempool时必须实际执行并通过回归 |
| 历史ticket验收记录与git历史 | 保留为历史证据；当前操作说明去掉已删除入口 |

删除后核对符号、导入、模式矩阵、fixture及启动脚本。残余的shadow文字仅可描述
历史记录，不能仍对应可执行分支。正式fetch范围/绑定/完成检查独立保留；它们不属于
旧host数值对照。READBACK旧配置的删除要有清晰的脚本迁移说明。

生产改动优先限制在Ascend/NPU目录；已有共享hook或全局配置若引用shadow专用符号，
必须同步清理。每项共享目录改动说明被删除的引用及为何仅修改后端不能完成，范围
控制为必要的定义和调用点，不借此重构其他后端。

## C. 整理检查、职责、命名与重复代码

- 删除没有独立约束价值的检查、重复分支、无用状态与不再使用的转换/封装。
  每项删除应说明由哪个边界已保证，或为何该状态已不可达。
- 静态配置、dtype、layout及拓扑约束尽量在初始化/peer注册时一次确定；跨进程输入
  和动态请求状态仍在相应边界检查。已有验证可复用时集中实现，避免多份规则漂移。
- 保留peer兼容性、attempt/generation、binding、可读范围、容量、stream完成和drain
  等必要约束及负向回归。不能仅凭正常路径gate通过就删除这些约束。
- 统一易混淆的变量/函数命名，区分row与slot、P与D、逻辑位置与物理地址、
  submitted与completed。注释解释约束和原因，删除过期的阶段占位说明。
- 合并重复推导和模式判断，明确对象的所有权与生命周期；不为了去重增加通用框架。
  热路径的诊断日志、快照、host同步和设备同步均核对必要性及性能影响。

按有界改动组织整改，每次保持正式/普通路径可验证。测试跟随行为及失效风险调整，
避免单纯为重命名增加镜像实现的测试。整改完成后复查完整目标版本与整改diff。

## D. 最终版本复验与交付

1. 执行Mac适用的静态检查、CPU行为测试及受影响组件gate，覆盖普通模式不依赖BM、
   正式模式无旧host调用，以及Graph输入/完成、复用、容量和传输契约。
2. 整理正式服务checker、启动脚本和说明：记录全rank模式、实际存储分配、发送类别/
   字节数、hit/P-miss/D-miss、实际Graph forward、drain与release结果。删除过期shadow
   命令及通过标记，不能将HTTP 200或来源计数当作数值正确性的证明。
3. 在最终清理版本重跑S5：真实P/D server和NPU Graph、完整mempool生命周期、用户
   curl小题目检查、同一已约定负载和目标下的性能测试。代码若因失败再次调整，重测
   受影响的验收项并明确最终版本证据，不能套用S5清理前的通过结果。
4. 同版本关闭mempool后，在NPU实际运行普通sparse PD短请求；核对原分配、main-KV
   传输、staging到host、D host写入和host miss读取。仅mock恢复或enum检查不能替代。
5. 汇总review与整改、文件职责、最终运行命令、版本、环境和实测结果；按
   [阶段验收流程](verification.md)取得用户确认后关闭03。

S6沿用S5的curl小题目输出检查，不额外增加正式数据集完整精度门槛。ticket04及后续
票的现有范围不改动。清理不得放宽S5已约定的性能目标；若测得退化，定位修复并重测。

## 完成条件

- [ ] review起点、目标版本、完整范围及两条审查结果可追溯，验收相关发现已整改并复查。
- [ ] 全部shadow专用代码/配置/脚本/测试删除，有用覆盖迁移，历史证据与当前说明区分。
- [ ] 冗余检查/代码及命名/可读性问题完成整改，必要动态边界与释放约束保留并验证。
- [ ] 最终版本通过S5全部验收，含用户curl输出确认及约定性能目标。
- [ ] 同版本普通sparse PD的实际NPU路径回归通过，不依赖BM初始化或MemFabric运行依赖。
- [ ] 文档、脚本、测试和交付证据对应最终代码，用户确认后才关闭Ticket03。

## 执行拆分（2026-10-06）

当前规划基线为`63590e9114e6d434291a3390bb103fbd26e3d1b9`，工作区在本轮规划前干净。
全量review范围从`4878a495d8bbacad52da8b75fd7b9685a32b8926`之后开始：
其下一个提交`5a87606304`引入独立mempool实现，必须纳入01–03审查。
不能从`a02bdc234e`的父提交开始，否则会漏掉最初独立实现。
`CONTEXT.md`中的`295132c4a5`仍是项目上游历史基线，与此次review起点用途不同。
实施时记录最终目标SHA，对区间中的其他改动按相关性分类，不机械重写整个区间。

### S6.1 全量review并形成整改清单

2026-10-06已执行，固定目标为`63590e9114e6d434291a3390bb103fbd26e3d1b9`。
详见[全量review报告](ticket-03-s6-review.md)：Standards三项设计整理建议，Spec两项
P2验证代码缺陷。首次审查完成，发现尚未整改，不勾选包含“整改并复查”的总完成项。

先审查固定范围，记录发现及严重度；分别输出Standards与Spec结果。
依调用关系核对五组：配置/资源与普通模式；BM布局/copy/writer；runtime/Graph/cache；
PD协议/传输/TP控制；scheduler/model runner及释放接入点。
查清每个检查保护的不变量、每份状态的所有者，以及初始化约束是否被重复放进逐token路径。
本轮规划扫描不替代正式review，也不预先宣称没有缺陷。

交付`ticket-03-s6-review.md`：固定起点/目标、范围、发现位置、影响、修复方案、
对应验证及复查状态。明确记录共享目录改动的必要性。

### S6.2 删除shadow和旧host参考readback

首先迁移仍有价值的行为测试，再删除专用代码，避免删除文件后同时丢失验证依据。
主要落点：

| 位置 | 计划动作 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | 删除两个SHADOW枚举、能力矩阵引用、readback参数及只为旧配置存在的验证入口 |
| `hardware_backend/npu/mempool/readback.py` | 删除旧selected KV与BM双路径比较实现；独立pattern数值验证保留 |
| `hardware_backend/npu/mempool/runtime.py` | 删除readback构造、比较/报告、completion快照字段及错误传播；保留正式fetch与write完成合同 |
| `hardware_backend/npu/sparsity_driven_kv_offload/attention.py` | 删除compare_selected_kv调用及对应旧分支 |
| `disaggregation/ascend/mempool_service.py` | 删除readback报告和专用异常处理；正式fetch故障处理继续保留 |
| `srt/environ.py` | 删除READBACK环境变量定义；此共享入口无法仅在NPU目录内删除 |
| `ascend-mempool-test/` | 删除verify_shadow_service.py及专用readback/shadow测试，迁移公共覆盖，更新resources/transfer模式矩阵和启动脚本 |

当前操作说明移除失效shadow命令，历史ticket和验收证据保留。独立KV pattern测试不能
因名字包含readback而整体误删。外部glm51mempool.sh亦需去掉READBACK导出；实施时核对
该外部仓库再形成独立修改/提交，保持用户要求的原有格式。

### S6.3 按职责清理并逐块验证

分三组有界改动：配置/资源；runtime/Graph/cache；PD传输/控制/释放。
重点检查runtime每forward的clone/CPU计数读取、服务tick的状态重复、peer layout校验
重复及散落模式判断。扫描到这些点仅代表review重点，不能未经分析直接删除。

静态形状、dtype、拓扑在构造/注册边界校验；请求身份、generation、绑定、可读范围、
容量、事件完成和drain保留动态约束。命名明确区分req_pool_idx、P/D物理slot、
submitted/written/completed；接口保持现有KVArgs，不引入此前否决的AscendKVArgs。
现有scheduler、prefill/decode、pool/model runner共享hook纳入审查；只有确有必要时
修改，避免借清理扩大普通后端的改动范围。

每组运行受影响CPU行为测试。重命名不新增镜像测试；实际发现的行为缺陷补充能复现
缺陷的回归。提交按可独立检查的改动组织，便于定位NPU回归，不将所有内容压成一次大改。

### S6.4 整改后复查与本地交付

复查全部01–03最终实现以及S6整改diff，关闭验收相关发现；验证剩余shadow引用
只属于历史证据。运行既有CPU suite、适用ruff/format/mypy及修改脚本的语法检查。
正式verify_service.py保留可审计完成/资源证据；删除或调整日志时同步修改checker，
不得通过删断言掩盖完成/复用合同的退化。

形成最终修改总结、review报告与用户可直接运行的NPU命令；记录测试实际执行范围，
Mac通过不标记为硬件通过。

### S6.5 用户执行最终NPU复验

沿用已通过的小容量部署和P=10.120.72.31、D=10.120.72.32：

1. 最终版本运行双机verify_fetch.py，检查eager/replay、连续五步、padding及数值。
   writer、资源或传输有改动时同时运行对应verify_graph/resources/pd_transfer gate。
2. 正式服务执行zero/decode/reuse三请求及verify_service.py；chat小题目明确关闭thinking。
   检查全rank资源、真实Graph、handoff、slot新generation和最终释放。
3. 用户确认清理后当前负载性能无退化；不外推长上下文容量。
4. 同版本关闭MEMPOOL，实际运行普通sparse PD：验证无BM初始化、原main-KV传输、
   staging/host写入与miss读取仍工作，核对正常输出及Graph执行。
5. 保存部署SHA、环境、命令、日志和用户确认，关闭03；S6结束后按原依赖推进04。

NUMA/驱动窗口与长上下文容量继续归09，由用户独立处理；不增加AIME26门槛，
也不提前实现05–07的完整压力/取消/故障矩阵。若review发现当前已支持行为的真实bug，
在本轮修复，不能以归属后续票为由遗漏。

## 本轮结果

用户授权执行S6第1步。已对30个提交、121个文件的完整开发增量完成双轴首次审查，
报告记录范围、五项发现、整改/验证方法及共享hooks的必要性。
Spec确认`verify_resources.py`仍要求正式mode报S5拒绝、registered启动测试仍期待
shadow；两者随S6.2覆盖迁移修正。Standards建议统一row推导、缩小control历史快照
与逐请求查询耦合、命名fetch统计字段，归S6.3。

Mac独立CPU suite 202项、严格mypy35文件、81个改动Python文件的AST及仓库hook
Ruff规则、67文件format检查通过；额外默认Ruff检查有43条未通过诊断，单独记录，未修复。
生产路径未发现其他可证实正确性缺陷；未执行完整registered suite或NPU测试。
本轮修改报告、计划及ticket Comments，保留生产代码待后续整改；S6与03尚未完成。
