# Ticket03 S5：正式服务接通与验收交付

日期：2026-10-05。实施基线：`8e00b36cf980159f9228bdd1c3440adc0c51f4a4`。
需求见[S5计划](ticket-03-s5-plan.md)和[ticket03](issues/03-prefill-direct-offload.md)。

**代码已实现，连续异步组件gate与三请求正式服务checker均已通过。**
2026-10-05的请求在全16 ranks完成512次replay并释放全部slot；算术结果正确，
但最终答复因512-token上限截断。2026-10-06双机连续五步异步组件gate的30个case
全部通过；同日三请求FORMAL_SERVICE_PASSED补齐全rank资源、Graph、slot复用和释放证据。
同日关闭thinking后的小题目返回正确完整答案；用户确认当前性能无问题，S5按已验证容量范围验收通过。
Ticket03保持open，S6已解锁但尚未开始。长上下文容量由用户独立跟进，当前不宣称已通过。

## 本次行为变化

`SGLANG_NPU_ENABLE_MEMPOOL=1`现在选择正式P/D模式。
P保留prefill计算所需的native HBM KV，并把prompt KV写入P BM。
D保留HBM Index K及sparse cache，attention的miss从P/D BM读取。
S3的零旧host KV/staging分配和S4的Index K/aux传输由正式入口接通。
当前BM使用DRAM，不能把BM与D的HBM sparse cache混为一层存储。

`MEMPOOL=0`继续选择原sparse PD模式。正式模式在资源分配和BM初始化前拒绝
`MEMPOOL_READBACK=1`，因为旧host参考已停用。shadow专用代码留待S6删除。
本次没有新增独立的host、传输或fetch组合开关。

## 生产代码：5个文件，均在Ascend/NPU目录

以下路径相对`python/sglang/srt/`。

| 修改路径 | 修改内容及作用 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | MEMPOOL开关返回正式P/D enum；解除阶段占位拒绝，提前拒绝正式模式与READBACK组合；保留模型、拓扑、dtype等实际限制。 |
| `hardware_backend/npu/attention/ascend_backend.py` | attach时核对mode与runtime fetch能力一致，正式D必须启用fetch；保留维度、设备和row数核对。 |
| `hardware_backend/npu/mempool/runtime.py` | 在现有逐层检查中记录有效selected KV、P miss、D miss；完成事件后累计到binding，生成`fetch_report()`。报告拒绝fault、旧binding及未完成forward。更新decode长度改用设备`fill_`，避免该处的标量host拷贝。 |
| `disaggregation/ascend/mempool_service.py` | 记录实际已分配buffer和已注册地址的资源统计；drain完成后、detach/native free前输出正式`fetch_result`，携带cancelled/drained及完成计数。 |
| `disaggregation/ascend/conn.py` | Index K和aux成功发送后记录实际路径的逻辑字节数与`main_kv_bytes=0`，供服务验收核对。原worker及handoff控制继续复用。 |

没有新增生产文件，也没有修改ModelRunner、scheduler、公共KVArgs或Mooncake worker。
`copy.py`的两次UniDexCopy、manager/attention的hit/refill及NPU Graph hooks均复用已有实现。

## 正式数据链路

```text
P native prefill计算 -> P BM写入 -> KV_READY
P Index K / aux原生传输 -----------> native transfer完成
                                      |
                          两项均完成后D才可decode
                                      |
D本层compact KV -> D BM写入 -> 本层可读长度
Index K -> top-k -> HBM sparse cache lookup
  hit  -> HBM cache -------------------------+
  miss -> P BM copy -> D BM copy ------------+-> selected KV
                                                |       |
                                            attention  HBM refill
```

P/D copy始终进入Graph，mask决定各自需要复制的位置。P copy负责prompt范围，
D copy负责decode范围。同一层的两次copy串行，hit与miss由现有stream处理。
固定inputs地址与capture时的selected buffer保持有效，replay只更新设备内容。
capture和padding使用无效binding，不消耗真实slot。

现有事件覆盖顺序保持为：binding更新→copy_ready→hit/miss完成→attention/refill，
attention返回前等待refill及slot map更新，runtime随后快照并记录completion event。
`poll_completed()`只消费已完成的快照，再累计请求统计。
正常decode没有新增逐token的设备同步；结束时仍执行whole-D drain。

## 完成、释放与日志的含义

```text
停止提交 -> drain -> 收集完成事件 -> fetch_result
         -> row detach -> native free -> release / DONE
         -> P释放对应BM slot并回复ACK -> D CLOSED
```

| 证据 | 能说明什么 |
| --- | --- |
| `mempool resources` | 实际native/HBM sparse/host/staging/Index K字节数，以及注册表中是否仍有main KV。正式D的native main KV、旧host与staging均应为0。 |
| `mempool native_copy` | 当前正式发送路径成功提交的Index K/aux逻辑字节数；不是网卡或底层协议的总流量计数。 |
| `mempool fetch_result` | 对应binding实际完成的forward、Graph replay、逐层覆盖、selected/hit/P miss/D miss计数。只有完成后才输出，不宣称与参考KV做了数值对照。 |
| `row_detach`、`native_free`、`DONE/RELEASE_ACK`和`free=16` | native资源回收及P/D BM租约释放的先后顺序和最终可用slot数量。 |

`selected_kv = cache_hits + prompt_misses + decode_misses`，这些计数跨层累计。
`written_kv`按实际提交/完成的forward计数；overlap可能多提交一步，不能用响应
`completion_tokens - 1`硬推占用。zero-decode合法地报告0个forward。

新增设备计数及完成快照有运行成本；是否达到性能目标必须用固定负载测量。
这些完成证据不替代独立pattern校验，也不替代用户的模型回答检查。

## 测试与交付脚本：9个文件

以下路径相对`ascend-mempool-test/`。

| 状态 | 路径 | 功能 |
| --- | --- | --- |
| 修改 | `scripts/verify_fetch.py` | 每轮输入预先放到NPU，连续提交5个forward及设备快照，最后一次同步再核对；覆盖P/D miss、mixed、all-hit、zero-valid、padding、完成前禁止detach及重绑。保留30个case、block_dim 24/48和capture/eager/replay目标地址回归。 |
| 新建 | `scripts/run_service.sh` | 统一P/D启动，提供formal与native对照；保留context1024、P/D各512、TP16、D Graph bucket16小容量配置；支持dry-run，记录版本、参数、环境和日志。 |
| 新建 | `scripts/verify_service.py` | 读取真实P/D日志，检查全16 ranks资源、传输、Graph、attempt/lease、两项readiness、完成/释放顺序、zero-decode、两次以上decode、物理slot新generation复用和最终free16；不会自动宣布精度或性能通过。 |
| 修改 | `scripts/verify_pd_transfer.py` | 补充解包后request非空断言，区分消息对象与序列化frames变量，修复扩大mypy范围时发现的S4脚本类型问题。六个S4测试场景不变。 |
| 修改 | `src/ascend_sparse/fixture.py` | 更新fixture说明，避免继续把正式服务称为未开放模式。 |
| 修改 | `tests/unit/test_sparse_config.py` | 正式P/D选择、READBACK拒绝及MEMPOOL=0兼容矩阵。 |
| 修改 | `tests/unit/test_fetch.py` | 使用延迟事件验证多个待完成forward、完成后报告、旧binding拒绝，以及row/slot重绑后的计数归零。 |
| 修改 | `tests/unit/test_pd_service.py` | 正式zero-decode走drain、报告、native free及DONE，旧shadow数值报告不会出现。 |
| 新建 | `tests/unit/test_service_gate.py` | 验证完整日志可通过；缺rank、错误统计、缺少P detach/free、错误释放顺序、旧存储及fault必须失败；覆盖D联合readiness两种顺序，以及P native释放后再发布ready。 |

## 文档与票面

| 状态 | 路径 | 内容 |
| --- | --- | --- |
| 新建 | `ascend-mempool-test/FORMAL_SERVICE.md` | 双机组件gate、P/D/router启动、三个生命周期curl、小题目、日志checker、普通sparse PD性能对照及回传清单。 |
| 修改 | `ascend-mempool-test/README.md` | 指向正式服务交付；区分当前formal入口与旧shadow历史命令。 |
| 修改 | `python/sglang/srt/hardware_backend/npu/mempool/README.md` | 更新正式模式、数据源、完成/资源日志及S5待硬件验收状态。 |
| 修改 | `.scratch/ascend-mempool/issues/03-prefill-direct-offload.md` | S5代码交付记录及当前阶段状态；保留open和硬件验收条件。 |
| 新建/更新 | `.scratch/ascend-mempool/ticket-03-s5-plan.md` | 保存此前用户确认的S5范围，并记录本轮实现交付。 |
| 新建 | `.scratch/ascend-mempool/ticket-03-s5-summary.md` | 本文，代码路径、执行链路、验证事实及后续步骤。 |
| 纳入已有修改 | `.scratch/ascend-mempool/ticket-03-s4-plan.md`、`ticket-03-s4-summary.md` | 此前用户确认的S4六个case通过及负向场景解释，不属于新增S4代码需求。 |
| 纳入已有新文件 | `.scratch/ascend-mempool/ticket-03-s6-plan.md` | 此前用户确认的S6全量review、shadow删除及可读性清理计划；本次没有实施S6。 |

Ticket04未修改。

## Mac实际验证

- 独立CPU完整suite：**195项通过**。
  `PYTHONPATH=ascend-mempool-test/src python -m unittest discover -s ascend-mempool-test/tests/unit -q`。
- 期间按配置、fetch、service、transfer、服务checker分别执行定向测试；先复现新增合同
  缺口，再修改实现。已通过的S2–S4组件和生命周期测试仍包含在完整suite中。
- mypy：**34个源文件通过**，覆盖独立包/脚本、mempool runtime/config以及Ascend
  protocol/control/tick/service。使用`ascend-mempool-test/pyproject.toml`配置。
- 评审后补充P侧日志顺序回归，先复现5个漏检的失败subtest，再修复checker；
  `test_service_gate.py`定向6项全部通过。
- 13个修改/新增Python文件通过Ruff、isort及语法检查；shell语法、4种role/mode的
  启动dry-run、`git diff --check`和84个文档本地链接检查通过，代码围栏闭合。

测试环境：Mac，`/private/tmp/ascend-mempool-s1`虚拟环境，Python3.9.6。
完整CPU结果保存于`/private/tmp/ticket03-s5-unit.log`。
本机没有NPU；未运行真实Graph、远端BM copy、完整server、curl或性能测试。

## 用户NPU反馈：首个正式服务请求（2026-10-05）

证据来自用户粘贴的P/D日志及curl完整HTTP响应，未读取NPU上的原始日志文件。
已交付SGLang提交为`fadc8223cd`，配套`glm51mempool.sh`提交为`24a6857`；
本次反馈未附两台NPU机器的实际Git SHA，不能据此确认部署版本。
P为`10.120.72.31`，D为`10.120.72.32`，两侧日志均包含rank0–15。
请求从P机器的`http://127.0.0.1:6699/v1/chat/completions`发出：

```json
{
  "model": "GLM-5.1-w4a8",
  "messages": [{"role": "user", "content": "A box contains 25 pencils. If 7 pencils are given away and the remaining pencils are divided equally among 3 students, how many pencils does each student get? Please explain your solution step by step."}],
  "temperature": 0,
  "max_tokens": 512
}
```

请求`room=6498740039664540360`，`attempt=506bc05162d7381522936c1da6c5d2b6`，
P/D slot均为0、generation均为1。P native row16、D native row2与BM slot独立。

| 时间 | 全rank证据及含义 |
| --- | --- |
| 18:16:29 | acquire、BOUND_ACK、start_prefill完成，双方对应同一个attempt。 |
| 18:16:30 | P发布KV_READY，prompt为47个token。 |
| 18:16:30–34 | 每rank成功发送Index K 2,555,904字节及aux 1,600字节，main_kv_bytes为0。 |
| 18:16:34 | D在KV_READY及native_transfer_ready之后start_decode，记录真实Graph replay；P完成native handoff并detach/free native row，BM仍为WAITING_DONE、free15。 |
| 18:17:47 | D返回HTTP 200；drain约131ms后输出completed报告，再detach/free及DONE，D free16。 |
| 18:17:48 | P收到DONE后释放BM、回复ACK；D进入CLOSED，双方全rank free16。 |

全部16个D rank的完成报告一致：`forwards=replay_forwards=submitted_kv=written_kv=512`，
`layers=78`、`layer_checks=39936`，`cancelled=false`、`drained=true`。
计数按每rank、跨层口径核对如下，不能把它们解释为请求数量：

```text
layer_checks  = 512 × 78                          = 39,936
prompt_misses = 47 × 78                           = 3,666
decode_misses = 512 × 78                          = 39,936
selected_kv   = 78 × sum(47 + t, t = 1..512)       = 12,120,576
cache_hits    = selected_kv - prompt_misses - decode_misses
                                                   = 12,076,974
```

计数与短上下文内每个KV首次miss、后续cache hit的行为吻合，命中率约99.64%。
这证明本次执行/完成计数一致，不是对KV数值的独立参考校验，也未覆盖长上下文淘汰。
P native资源在18:16:34释放，P BM直到18:17:48才释放，符合两类存储的持有期限。

HTTP返回`prompt_tokens=47`、`completion_tokens=512`、`total_tokens=559`、
`finish_reason=length`。生成文本多次给出`25 - 7 = 18`及`18 / 3 = 6`，算术结果正确；
但大量思考/草稿文本和字面量`</think>`仍在content中，最终答复在`25 - 7`处截断，
`reasoning_content=null`。本地启动脚本没有设置reasoning parser；服务端只有在parser
配置生效且separate_reasoning启用时才拆分字段。这与未拆分思考文本相符，尚未核对
用户实际启动参数及权重chat template，不能将其认定为mempool数据错误。
拆分reasoning字段本身不会减少生成token，也不会解决长度截断。

已交付的小容量配置D BM每slot为512，本次written_kv已到512；补测优先缩短回答，
不能仅把max_tokens提高到1024/2048。若要关闭thinking，应核对实际权重template
是否支持`chat_template_kwargs.enable_thinking=false`；提高容量需同步P/D配置及context限制。

稳定阶段server滚动吞吐为6.92–6.97 token/s，对应单请求约143–145ms/token的粗略量级。
首条0.07包含此前长时间空闲，不能视为稳定性能。请求未开启stream，日志无法给出
客户端准确TTFT/TPOT；用户性能判断仍待反馈，不能据此宣布性能达标。
18:15:53的400属于另一请求，未提供错误响应body；FastAPI警告发生于构造错误响应时，
不能把弃用警告当作400的根因。

**10-05当时结论：单请求正式Graph链路、统计及资源释放已有NPU证据；S5尚未整体验收。**
仍需完整小题目回答和用户确认、zero-decode/连续请求/新generation复用的服务checker、
S5连续五步异步组件gate结果，以及实测性能确认。当前片段不包含启动资源及capture
日志，完整服务checker还需启动日志。保留Ticket03 open，不解锁S6。

## 用户NPU反馈：连续异步fetch组件gate通过（2026-10-06）

用户在P `10.120.72.31`与D `10.120.72.32`的device0执行
[验收说明](../../ascend-mempool-test/FORMAL_SERVICE.md)第1节的`verify_fetch.py`命令，
于11:55:26–30完成。配置为S_P=8、S_D=16、2层、1head、KV dim576、Graph rows16、
active rows3、top-k2048、block dims24/48、replay cycles2、warmup3。

| 核对项 | 用户回传结果 |
| --- | --- |
| 双机结果 | P/D均输出ALL_CHECKS_PASSED，D终端已返回shell |
| case矩阵 | 2个block dim ×（1轮eager + 2轮replay）× 5种case = 30条FETCH_PASS |
| 连续异步执行 | 每项queued_forwards=5；每组五次forward连续提交后统一同步，共6组，不是每条PASS另运行五次 |
| 数值检查 | 每个case逐元素核对37,748,736个BF16元素，包含有效行与padding |
| 每层copy计数 | P miss=[6,0]；D miss=[0,6]；mixed=[6,6]；all-hit/zero-valid=[0,0] |
| 完成与回收合同 | 脚本包含未收集完成时detach必须失败、五次forward完成报告、detach/重绑及pool/SDK清理检查；均未触发失败 |

D初次查询P的GVA出现转换失败，随后完整MAPPED及所有fetch检查通过，未形成持续
映射故障。可选扩展库、tag/key、store响应及base-format提示未阻断本次检查；本轮无
Traceback或失败清理报告。成功标记在pool/SDK/channel清理及报告写入之后才输出。

报告路径：P `/tmp/ticket03-s5-fetch-p.json`、D `/tmp/ticket03-s5-fetch-d.json`；
日志路径分别为同目录的`ticket03-s5-fetch-p.log`、`ticket03-s5-fetch-d.log`。
证据来自用户粘贴的终端输出，agent未读取远端原始文件；本轮部署SHA和依赖版本
未另行提供。以上是用户NPU执行结果，不是agent在Mac重跑的结果。

**该组件gate通过。** 它补齐连续异步copy/cache/完成合同的独立数值证据；当时尚待
真实服务checker、完整小题目回答和性能确认。随后收到的服务结果见下一节。
Ticket03保持open，S6未开始，NUMA调查保持封存。

## 用户NPU反馈：三请求正式服务checker通过（2026-10-06）

用户顺序发送`/generate`原始文本请求：hello（1 token）、天空为何蓝（32 tokens）、
冰为何浮于水（32 tokens）。temperature0、ignore_eos=true、stream=false，三次实际
completion_tokens均符合要求，finish_reason均为length。
输入/响应保存在`/tmp/ticket03-s5-requests/`，分别为zero/decode/reuse-request.json与
zero/decode/reuse.json。强制长度用于生命周期测试，不要求这些截断文本构成完整答案。

用户在P的`/home/cryang/sglang`运行`verify_service.py --prefill-log ../p.log
--decode-log ../d.log --requests 3 --layers 78
--report /tmp/ticket03-s5-formal/service-result.json`，输出FORMAL_SERVICE_PASSED。
有效日志路径为`/home/cryang/p.log`与`/home/cryang/d.log`；此前使用当前目录p.log/d.log
的输入由用户Ctrl-C取消，不能算一次checker失败。

回传JSON为status=formal_service_passed、requests3、ranks_per_side16、
zero_decode_requests1、decode_requests2。每侧16条资源记录各自一致，数值均按每rank：

| 资源字段（bytes除注明项） | P | D |
| --- | ---: | ---: |
| host_kv_bytes | 0 | 0 |
| staging_bytes | 0 | 0 |
| transport_staging | false | false |
| registered_main_kv_entries（条） | 0 | 0 |
| registered_index_k_entries（条） | 78 | 78 |
| native_kv_bytes | 15,044,050,944 | 0 |
| index_k_bytes | 3,343,122,432 | 18,384,617,472 |
| sparse_cache_bytes | 0 | 3,128,426,496 |

这些是已创建buffer的资源统计；host_kv_bytes=0表示旧长期host KV没有分配，
不表示BM DRAM为0，也不是模型权重及全部进程内存的总量。
每rank三请求累计native发送Index K 7,667,712字节、aux4,800字节；checker同时要求
记录的main_kv_bytes为0。资源注册和实际传输均满足正式模式的检查合同。

按当前checker合同，通过还意味着两侧全rankmapping就绪、D capture/replay有记录、
三请求的matching binding与联合readiness成立、fetch报告及drain/释放顺序正确、
全rank P/D物理slot均出现新generation复用，最终free16。JSON是检查汇总，未携带
各room/attempt/slot/generation及forward明细，不能据此补造具体编号或次数。

输出内容方面，天空解释被32-token上限截断；冰浮水响应含“Advertisements”和
多语言文本。请求未通过chat messages接口，且强制生成固定长度；这些结果不能充当
完整回答通过证据，也不能仅凭其形式确诊mempool KV污染。下一步使用chat接口的简短
算术题核对完整回答，若仍异常则定位模板/生成配置及数据路径。checker检查执行与
生命周期，不做参考KV数值比对，也不自动认可输出精度。

证据来自用户贴出的HTTP结果、checker输出和完整JSON；未直接读取远端原始日志，
实际部署SHA及本轮环境版本仍未另行提供。报告中的accuracy与performance均为pending。
**正式服务资源、Graph、zero-decode、连续请求复用及释放检查通过；S5仍待完整回答、
性能确认和最终记录。** S6未开始，Ticket03保持open。

## Standards

未发现阻塞性规范违反。生产修改局限于Ascend/NPU，保持open及用户NPU验收流程。
评审指出2处文档状态不一致：S6计划仍称S5待实施，S4总结未标明旧shadow启动是
历史状态；均已更正。1项非阻塞维护建议为给runtime统计列命名，归S6可读性清理。

## Spec

发现1项P2：服务checker原先只核对P侧事件存在，遗漏detach/free和关键顺序。
已补充`BOUND_ACK → start_prefill → row_detach → native_free → native_release → DONE`
检查；ready可在native_release前后，但必须位于start_prefill与DONE之间。
补充并通过针对性回归，规格评审已复核确认修复，无遗留规格问题。
其余未发现确定的生产正确性问题或范围扩张。

规范评审：0项阻塞、2处文档更正、1项S6维护建议；规格评审：1项P2已修复并复核。

## 用户NPU反馈：关闭thinking后小题目输出检查通过（2026-10-06）

用户在同一chat请求中增加`chat_template_kwargs={"enable_thinking": false}`，
其余参数保持不变：入口`http://127.0.0.1:6699/v1/chat/completions`，
模型`GLM-5.1-w4a8`，temperature=0，max_tokens=256，stream=false。
输入：“小明原有17个苹果，又买了25个，送给朋友9个。现在有多少个苹果？
请只用一句话给出算式和答案。”

此前响应在思考/起草过程中用满256 tokens，finish_reason=length；本次返回：

> 17+25-9=33，现在有33个苹果。

响应id=`f76f25e630124d00a3e08c06ed7ec14d`，created=1791264302，
prompt_tokens=35，completion_tokens=15，total_tokens=50，finish_reason=stop，
matched_stop=154827，reasoning_content=null。算式、答案及一句话格式均正确，未截断。
据用户回传结果记录S5小题目输出检查通过；该结果支持此前截断与thinking及输出上限有关，
不外推为正式数据集精度或逐token baseline通过。证据为用户粘贴的响应；本次未提供
新的P/D日志，Graph及生命周期证据仍引用前述独立服务checker。性能确认、部署SHA及
最终环境记录仍待补齐，S5整体未关闭，S6未开始。

## 2026-10-06：用户确认当前性能，S5按已验证容量范围验收

用户确认“性能目前没有问题”；长上下文承载能力因NUMA相关问题仍待确认，
由用户另行解决设备驱动/硬件相关问题，不作为S5剩余阻塞项。结合已通过的
连续异步fetch gate、三请求全rank正式服务checker和关闭thinking后的正确完整回答，
记录S5在当前已验证的小容量配置下验收通过，解锁S6；本轮不启动S6实现。
性能结论来源为用户人工确认，未回传完整TTFT/TPOT/吞吐测量表，不补造数值，
不宣称16K或更长上下文容量、性能及完整数据集精度已获验证。
实际部署SHA、完整环境快照与性能原始数据仍是归档缺口，保留待补说明；
不将这些缺口写成已采集，也不重复要求已获用户确认的性能验收。
Ticket09继续封存，由用户独立跟进；Ticket03仍为open，待S6全量review、
shadow删除、清理及最终版本复验。本轮仅更新阶段文档，未运行NPU或修改生产代码。

## 下一步

S5当前已验证范围验收通过，下一阶段为[S6](ticket-03-s6-plan.md)：全量review、
删除shadow专用代码、清理冗余检查/命名，并在最终清理版本重跑正式服务验收与普通模式回归。
实际部署SHA、完整环境快照及性能原始指标仍待归档；不影响本次用户确认的阶段推进。
长上下文容量问题保留在09，由用户另行处理。Ticket03在S6完成前继续保持open。
