# Ticket03 S5：正式服务 Graph、curl 输出检查与性能验收

日期：2026-10-05。代码基线：`8e00b36cf980159f9228bdd1c3440adc0c51f4a4`。
S4双机NPU gate已获用户确认。2026-10-05按用户决定更新验收范围，随后获授权实施S5。
本方案的正式入口、完成证据、连续异步gate及服务交付已实现，等待用户NPU验收。
Ticket03仍为open；实现清单见[S5总结](ticket-03-s5-summary.md)。
需求来源：[ticket03](issues/03-prefill-direct-offload.md)、[spec](spec.md)和
[阶段验收流程](verification.md)。

**Blocked by:** S4实现及用户NPU验收，已满足。

## 目标与现有基础

让S1–S4接成可启动的正式服务：D attention使用BM fetch的selected KV，
Graph可跨请求复用，stream依赖和完成事件覆盖全部KV访问，释放后才允许复用存储。
验收包含完整mempool链路、用户通过curl小题目核对输出，以及约定性能目标达标。
S5不加入AIME26等正式数据集的完整精度验收；ticket04内容保持不变。

| 已有能力 | S5需要补齐或核对的部分 |
| --- | --- |
| S2两次UniDexCopy、固定inputs、HBM hit/refill及逐层检查 | 服务capture/replay入口、连续异步提交、不同请求复用 |
| S3正式模式零旧host KV/main-KV staging | 正式ModelRunner初始化真正选择该模式 |
| S4仅Index K/必要辅助传输、联合readiness | 与真实Graph、native row回收和BM持有期限联动 |
| 02 forward hooks、binding事件、whole-D drain与DONE/ACK | 验证这些事件覆盖正式fetch、cache refill和slot map |

实施前`MEMPOOL=1`返回shadow模式；`validate_runtime_support()`拒绝正式服务。
原`verify_fetch.py`已验证真实BM writer/fetch和Graph，但每个case后同步设备。
这份证据覆盖单步数据正确性，连续提交和服务生命周期需要补充验证。
当前BM是DRAM存储，HBM sparse cache是D侧的独立缓存。

## A. 固定Graph输入及capture边界

复用`SparseCopyInputs`、`KVFetch`和现有NPU Graph hooks。

- BM映射及runtime固定表先就绪，再warmup和capture。控制握手在请求准入前完成。
- 按layer、Graph batch bucket、top-k形状保留inputs。内容用设备`copy_`更新。
- 保持BM layer base、inputs、HBM cache和captured selected buffer地址稳定。
- capture/warmup使用无效binding；row0及padding不获取slot、不访问有效KV。
- P/D两次copy始终进入Graph；某一路没有miss时用mask表示，不在Python中省略调用。
- 同形状eager换目标buffer后，旧Graph仍写原capture目标；不能替换旧Graph持有的inputs。
- replay的slot、长度和top-k来自当前设备输入，不依赖capture时的Python请求值。

硬件gate沿用Graph width16、top-k2048；可变有效top-k用mask和-1补齐。
现有lookup kernel固定宽度2048，S5不假定其他物理宽度可在NPU上执行。

## B. 核对stream与完成事件

沿用现有事件。先用回归验证顺序，只修改出现缺口的入口。

```text
binding更新 -> binding event -> 本次forward提交stream
本层D BM write + 路由metadata -> copy_ready
  hit stream  -> HBM hit copy                  -> hit_done
  miss stream -> P BM copy -> D BM copy         -> miss_done
  map stream  -> slot-map更新                  -> slot_map_done
hit_done + miss_done -> attention读取selected KV
hit_done + miss_done -> HBM cache refill        -> refill_done
attention结束 + refill_done + slot_map_done
  -> runtime快照与completion event -> poll_completed
```

`copy_ready`也保护hit所需的索引；miss stream内串行提交P/D copy。
attention在开始计算前等待hit/miss，返回前等待refill/map。
runtime在forward/replay返回后记录完成事件，借主stream汇合覆盖全部KV工作。
逐forward的设备计数先快照，事件完成后再读回；后续forward不能抹掉上一轮错误。

常规decode不新增逐token设备同步。实际NPU验证应连续提交至少两个forward，
最后才收集结果，检查同一Graph输入更新及HBM缓存复用的正确性。

## C. 释放、row复用与容量

继续使用现有service/tick；不另建请求状态机。

| 资源 | 释放条件 |
| --- | --- |
| P native KV页、Index K源页及handoff metadata | 原handoff完成且本地BM写完，或失败路径确认native drain；之后detach row并走原allocator |
| P BM slot | D各rank完成drain并发出对应DONE，P各rank完成本地写入和必要detach；再释放并回复ACK |
| D native页、HBM cache映射和request row | 停止新提交，处理overlap结果及延迟sampling，设备工作完成；失效binding并执行原清理 |
| D BM slot | 各rank drain及本地释放一致成功后归还；保留终态记录等待RELEASE_ACK |

正常结束、取消和zero-decode均走相同安全条件。HTTP完成、CANCEL或KV_READY均
不能替代drain。P native row可被新请求使用时，旧P BM slot仍可能属于上一请求。
复用request row要清slot map；复用BM slot要安装新的binding并重置计数。

检查P prompt容量、D实际提交的KV行数、总context和Index K可寻址范围。
沿用runtime对每次提交上界的验证，在越界写入前失败。
P首个采样token尚无decode KV；overlap可能提交额外forward。
因此从实际submitted/completed判断占用，不写死输出数减一或加一。

## D. 开放正式启动并明确READBACK语义

在A–C及CPU合同验证完成后，复用现有开关开放正式路径：

| 启动配置 | 计划行为 |
| --- | --- |
| sparse开启、`MEMPOOL=1`、P角色 | `PD_PREFILL_MEMPOOL`；保留P native KV，直接写P BM，只传Index K/辅助数据 |
| sparse开启、`MEMPOOL=1`、D角色 | `PD_DECODE_MEMPOOL`；不分配旧host/staging，启用runtime fetch |
| sparse开启、`MEMPOOL=0` | 恢复原P/D sparse PD路径，不要求初始化BM或导入MemFabric运行依赖 |
| 正式模式且`MEMPOOL_READBACK=1` | 在分配/跨机初始化之前明确拒绝；旧host参考已停用 |

不增加独立的关host/关传输/选fetch组合开关，也不新增shadow启动入口。
S5先切换正式入口；[S6](ticket-03-s6-plan.md)统一删除shadow专用模式、READBACK
配置/实现及配套代码，迁移仍对正式/普通模式有用的测试。当前shadow代码属于过渡实现。
模式只在进程启动时选择，切换需要drain后重启P/D并重新capture。

移除笼统的“S5未完成”拒绝后，实际配置、拓扑、dtype、READBACK和依赖校验继续生效。
backend attach应验证formal D确实获得fetch runtime；缺失时直接失败。
formal模式只报告实际forward、fetch检查和释放事实，不生成shadow数值对照通过结论。

## E. 用户curl小题目检查

由用户使用curl向真实P/D服务的请求入口发送一个答案明确的小题目，并人工核对回答。
交付命令使用当前服务实际支持的API和请求格式；需要流式输出时一并给出收集方式。

- 记录代码版本、模型、原始题目、采样/输出长度参数、预期答案、实际回答及用户结论。
- 请求应产生真实decode forward；结合该请求的Graph、fetch和释放证据确认正式链路。
  zero-decode与连续请求/slot复用另行作为生命周期测试执行。
- 回答应正确且无异常乱码、无故截断等现象；异常须定位修复后重测。
- 这是小题目的输出检查，不要求正式数据集评分、完整精度统计或逐token baseline对齐。
  独立已知pattern的KV测试仍用于验证copy数据，不能据一次小题目宣称完整模型精度通过。

## F. 性能验收

S5继续要求预期性能达标。按用户最新决定，验收时由用户查看实测TTFT、TPOT和输出
吞吐并人工判断，当前不要求预先提供数值阈值。交付固定负载的测量步骤；同版本普通
sparse PD保留为诊断对照。记录实测条件、结果及用户确认，不能自行宣布性能通过。

| 项目 | 需要确定或记录的内容 |
| --- | --- |
| 诊断对照 | 同版本普通sparse PD；记录双方模型、硬件、TP/PP、Graph配置和资源差异 |
| 负载 | 固定prompt/输出长度、采样参数、并发、请求数量、预热与测量轮数；具体值在交付命令中确定 |
| 请求延迟 | 记录TTFT、TPOT或ITL及其统计口径，避免将Graph初始化混入稳态测量 |
| 生成吞吐 | 在明确的有效测量窗口内计算token/s，说明并发及完成token计数 |
| 资源与收尾 | 记录HBM/host/BM占用，以及drain/释放时间；核对旧重复存储及main-KV流量确实停用 |
| 验收结论 | 用户验收时查看实测指标并判断是否满足预期；记录人工结论，不设置待提供阈值的前置条件 |

两种模式可使用各自可运行的容量配置，但负载必须同时适用，配置差异必须记录。
若采用其他已确认基线，写清对应版本和测量条件。日志包含长时间idle时，不能直接把
server滚动吞吐当作该请求的性能。定位到退化或接线缺口时在S5修复，完成后重测。
**尚未测量或尚未获用户性能确认时，不得将S5性能标为通过。**

## 拟修改与复用的代码入口

以下路径相对`python/sglang/srt/`。表中“核对”不等于必须修改该文件。

| 文件/入口 | 计划动作与职责 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | 必改：正式mode选择、替换阶段保护、提前拒绝不兼容READBACK |
| `hardware_backend/npu/mempool/runtime.py` | 正式runtime工厂、binding/forward完成合同、独立于readback的最小请求完成证据 |
| `hardware_backend/npu/attention/ascend_backend.py` | 核对attach模式一致性和本层writer在materialization之前；按缺口补校验 |
| `hardware_backend/npu/mempool/copy.py` | 核对inputs/copier持有和目标身份；以多次capture/eager/replay回归保护 |
| `hardware_backend/npu/sparsity_driven_kv_offload/manager.py`、`attention.py` | 核对既有事件汇合、cache/map刷新及attention消费；只补实际缺口 |
| `hardware_backend/npu/graph_runner/npu_cudagraph_backend.py`、`npu_graph_runner.py` | 核对warmup/capture和replay外层scope、无效rows及固定设备输入 |
| `disaggregation/ascend/mempool_service.py` | 正式完成证据、容量与native/BM释放顺序；复用whole-D drain和allocator入口 |
| `disaggregation/ascend/mempool_tick.py`、`mempool_control.py` | 原有TP一致性与slot状态机作为复用依赖，通过生命周期测试覆盖 |

生产改动优先限制在Ascend/NPU目录。当前已有共享hook覆盖runtime初始化、eager
forward、调度prepare、handoff和defer_release，暂未发现必须新增主线hook的依据。
不预先修改ModelRunner、scheduler、公共KVArgs、utils或Mooncake worker。
若测试证明现有hook缺失，需说明触发场景、缺口及为何无法在Ascend/NPU内完成，
再提出最小主线修改。

## 验证与交付顺序

1. 在现有CPU测试补正式启动、Graph边界、延迟完成、复用和容量回归。
   重点文件为`test_sparse_config.py`、`test_runtime.py`、`test_fetch.py`、
   `test_materialize.py`、`test_pd_service.py`和`test_pd_tick.py`。
2. 扩展现有`verify_fetch.py`，覆盖无逐步host同步的连续replay、完成后poll、
   request row与P/D slot复用、同形状目标切换和padding。数据使用独立已知pattern。
3. 在已验证事件/生命周期合同后开放正式模式，完成Mac适用检查及阶段评审。
4. 交付上述组件NPU gate和真实正式服务的P/D命令、curl请求、日志与失败判据。
   使用现有小容量context1024、P/D各512、TP16、Graph width16配置及用户本地GLM-5.1。
   正式服务至少包含zero-decode、真实decode和连续请求实际slot复用，用户核对小题目输出。
5. 按明确的负载和测量口径运行性能测试，交付指标，由用户验收时判断是否达标。
   记录实际代码版本、软硬件环境、命令、日志和用户反馈，修复失败项后重测。
6. 用户确认功能、curl输出检查和性能全部通过后记录S5完成，解锁S6全量review、
   shadow删除、代码清理和最终版本复验。

S5关键判据：capture零真实slot访问；replay使用当前绑定；attention/cache消费正确
selected KV；未完成时不能detach/free；最终各rank的BM free=16；无旧host回退。
最小服务smoke需要真实Graph及释放证据，HTTP 200本身不算通过。
S5实现需提供本阶段可直接使用的formal启动配置、完成日志和检查步骤，覆盖模式、
分配、传输、读取来源及生命周期。S6继续整理checker、启动脚本和普通模式完整回归，
并在最终清理版本重新执行S5验收。完整精度不纳入S5；ticket04保持现有内容，系统
容量/取消/故障矩阵仍归后续票。

## 完成条件与交付证据

- [ ] 正式P/D server启动，真实NPU Graph capture/replay下attention消费正式fetch结果。
- [ ] 实际分配和发送证据满足旧host KV/staging/main-KV流量为零，必要Index K/aux正常。
- [ ] 连续异步replay、请求/slot复用、padding和zero-decode验证通过；全rank最终free=16。
- [ ] 用户用curl完成小题目检查并确认回答正确，输入/参数/输出记录完整。
- [ ] 记录性能测量条件与实测指标，用户查看后确认满足预期。
- [ ] 记录实现评审、版本、环境、命令、日志及用户NPU验收；S6才可开始实施。

## 本轮结果

本轮按用户授权实现S5：正式模式选择、READBACK早期拒绝、backend fetch模式核对、
完成后fetch报告、实际资源/传输日志，以及连续五步异步组件gate。
交付[启动/验收说明](../../ascend-mempool-test/FORMAL_SERVICE.md)、P/D启动脚本和正式
日志检查器。Mac检查与评审记录见[S5总结](ticket-03-s5-summary.md)。
等待用户执行NPU组件与真实服务gate、curl小题目和性能验收，未勾选硬件完成条件。
性能按用户最新要求在验收时查看TTFT/TPOT/输出吞吐并人工判断，不要求预先提供阈值。
S4已验收状态不变，S6未开始，ticket04未修改。
