# 10: GLM-5.2 算法适配与 indexer 跨层复用

**What to build:** 在03已验收的正式mempool路径上适配用户提供的GLM-5.2配置，
重点支持部分层计算top-k、其余层复用索引。核对现有算法接线并补齐缺口，保持每层
独立compact KV、按实际indexer层分配/传输Index K，以及真实NPU Graph执行正确。
同一权重、相同推理参数下，对照普通sparse PD与mempool，并回归GLM-5.1。

**Parent:** [Ascend mempool spec](../spec.md)

**Blocked by:** [03: 正式 mempool 数据路径 cutover](03-prefill-direct-offload.md)（已关闭）。

**Blocks:** [04: 正式 mempool Graph 与模型集成验收](04-single-request-graph-decode.md)。

**Status:** ready-for-agent

**State:** open

## 执行顺序与范围

用户要求在继续04之前优先适配GLM-5.2；当前顺序为03 → 10 → 04。
本票先审计已有实现与实际模型配置的差距，不重新开发仓库已经支持的跳层逻辑。
生产修改以已复现的算法、存储、传输或Graph缺口为依据。

本票覆盖主模型、TP16/PP1、普通非投机decode。配置包含NextN/MTP字段，但这不代表
启用投机解码；首轮明确关闭MTP，不把MTP推理/跨iteration共享支持隐含加入验收。
若实际部署要求启用MTP，先补充对应算法及验收范围。

P/D miss多stream并行建议仍归04，本票不同时改变copy调度。
NUMA/驱动窗口及大容量调查仍由用户在09独立跟进；本票不承诺百万token上下文，
也不包含08的AIME26或05–07完整并发/取消/故障矩阵。

## 用户提供的模型依据（2026-10-07）

来源为会话中粘贴的`/data_lib/data/models/glm-q/config.json`及
`quant_model_description.json`前80行，agent未读取远端权重。
模型名称按用户提供的GLM-5.2记录，不根据文件夹名称猜测版本。

| 字段 | 提供值 / 含义 |
| --- | --- |
| architecture / model_type | `GlmMoeDsaForCausalLM` / `glm_moe_dsa` |
| num_hidden_layers | 78，主模型层号0–77 |
| indexer_types | 21个full、57个shared；显式列表优先于freq/offset推导 |
| full层号 | `0, 1, 2, 6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70, 74` |
| index_topk / freq / offset | 2048 / 4 / 3；index_topk_pattern为null |
| compact MLA KV | kv_lora_rank=512，qk_rope_head_dim=64，合计576维 |
| Index K | index_head_dim=128，index_n_heads=32 |
| RoPE | rope_interleave=true，indexer_rope_interleave=true；theta=8000000 |
| NextN/MTP | num_nextn_predict_layers=1，index_share_for_mtp_iteration=true |
| dtype / 最大位置 | 声明BF16；max_position_embeddings=1048576不代表已验证容量 |

量化描述已展示Attention投影与indexer.wq_b的`W8A8`、前两层MLP的
`W8A8_DYNAMIC`及部分参数`FLOAT`。后续MoE专家格式尚未提供，实施时须核对完整
量化描述和实际加载路径。权重量化不用于推断KV dtype；本票沿用现有BF16 compact KV
合同，若真实配置不兼容须明确报告。

## 算法与存储合同

1. `full`层运行自己的indexer并产生token位置；`shared`层复用同一forward最近的
   producer结果。例如3/4/5复用2，7/8/9复用6，75/76/77复用74。
2. 共享的是token位置，不是KV内容或selected KV buffer中的前层结果。
   每层仍计算、写入并读取自己的compact KV：P/D BM和D sparse cache覆盖全部78层。
3. 在本票主模型、无MTP且使用紧凑Index K布局时，只需21层Index K。
   显式model layer ID与紧凑buffer slot分别管理，不能把slot序号当model layer ID，
   不能把21当作BM层数或forward完整性检查的层数。
4. prefill即使因短序列不执行稀疏top-k，也必须为后续decode准备producer的Index K。
   shared层没有自己的indexer权重时，不能因索引传递丢失而重新计算未初始化indexer。
5. reuse不得跨request/forward误用旧索引；Graph replay使用当前输入及固定metadata地址，
   有效性mask、P/D边界、binding、written prefix和padding约束继续生效。
6. Index K/native handoff按实际层集合匹配P/D；main compact-KV仍从BM读取。
   不匹配的peer布局在服务准入前被拒绝，不能仅比较总层数就放行。

## 已有入口与实施顺序

以下为当前仓库已有能力或需审计入口，不表示GLM-5.2已经验收通过。
路径相对仓库根目录。

1. **确认配置及加载。** `python/sglang/srt/models/glm4_moe.py`中的
   `GlmMoeDsaForCausalLM`继承DeepSeek DSA路径；核对
   `models/deepseek_v2.py`、`models/deepseek_common/deepseek_weight_loader.py`和实际
   W8A8加载。复用`configs/model_config.py::dsa_layer_skips_topk`的配置规则。
2. **核对top-k传递。** 审计
   `hardware_backend/npu/modules/deepseek_v2_attention_mla_npu.py`及共享MLA forward、
   层间返回值和启用的overlap路径；修复producer/consumer传递缺口。
3. **核对两类层布局。** `model_executor/pool_configurator.py`、
   `hardware_backend/npu/memory_pool_npu.py`已有indexer layer集合/slot映射。
   检查容量估算、分配、Index K写读及普通/正式PD传输；
   `disaggregation/ascend/conn.py`已有显式layer ID和Index K-only formal handoff。
4. **核对mempool与Graph。** 审计`hardware_backend/npu/attention/ascend_backend.py`、
   `hardware_backend/npu/mempool/runtime.py`和sparse cache路径，保持每层writer/fetch，
   以及覆盖所有层和异步访问的完成证据与drain。
5. **补测试及交付。** 扩展现有独立gate、配置测试和服务checker；检查参数不再把
   model layer数与indexer layer数混用。交付逐机启动、baseline和结果比对命令。

## Acceptance criteria

- [ ] 使用提供配置验证21个producer和57个shared层及完整复用链；配置入口保持通用，
  不按模型目录名硬编码。非法或无法满足的共享关系给出明确错误，不静默读取旧索引。
- [ ] 主模型W8A8加载成功，shared层无indexer权重不触发未初始化计算；
  核对完整量化描述、RoPE配置和实际backend，记录部署版本与配置。
- [ ] 普通与mempool两种模式均保持78层独立compact KV；紧凑Index K布局使用正确的
  21个model layer ID。资源/传输报告分别记录模型层数和Index K层数。
- [ ] 受控数值用例使用每层不同的KV pattern，验证shared层沿用同一token位置但取回
  自己的KV；覆盖P-only、D-only、mixed、hit、zero-valid和padding。
- [ ] eager与Graph capture/replay均通过；Graph batch width至少16。
  连续forward、请求切换、row/slot复用不会沿用前一个forward/request的top-k或KV。
- [ ] 真实GLM-5.2请求覆盖有效序列长度小于、等于、大于index_topk=2048，验证
  真正需要稀疏选择时的producer/shared路径；只通过短请求不算本票完成。
- [ ] 对照相同GLM-5.2权重、输入、tokenizer/chat模板、greedy采样、thinking和
  非投机配置的普通sparse PD与mempool输出token序列；差异定位解决，不只比较HTTP状态。
  普通baseline先做合理输出检查，不能把两条路径同错当作正确。
- [ ] 真实P/D请求通过handoff、Graph decode、drain、DONE/RELEASE_ACK及slot归还；
  21层Index K的handoff不能导致78层KV完成检查误判。
- [ ] 同版本GLM-5.1正式mempool + Graph快速冒烟通过（用户2026-10-11简化回归范围）；记录GLM-5.2性能观测，
  不预设跨模型相同速度或固定加速比例。
- [ ] 完成Standards/Spec review、本地检查、用户代码核对和用户NPU验收，
  按[阶段交付流程](../verification.md)记录证据后关闭10并解锁04。

## Verification

Mac执行配置/映射/协议/生命周期的适用CPU测试和静态检查；硬件验证由用户在NPU执行。
测试必须区分模型总层数、实际indexer层数、Graph batch width与真实KV slots。
独立数值基准使用已知pattern，避免比较同一实现产生的两份错误结果。

先运行组件gate，再用同权重普通sparse PD建立baseline，最后运行mempool并比对。
逐机命令须给出实际CLI、环境、P/D顺序、Graph设置、关闭投机的配置、模型和报告路径。
为>2048用例计算P/D容量、Index K HBM及对齐后BM用量，采用满足测试的最小可运行配置，
不直接套用03的context1024。若硬件无法覆盖该用例，明确保留该项待验收，不把09
重新设为主线依赖，也不以短序列替代真实稀疏选择验证。

回传P/D SHA、模型config与完整量化格式摘要、启动参数、producer层映射、组件数值报告、
Graph/service日志、baseline/mempool token序列及差异、性能数据和用户确认。

## Comments

### 2026-10-11：补记用户单机短请求反馈，继续作为下一实施票

用户在建立本票后的会话中回传`glm52.sh`运行结果，并明确说明是单机P/D collocation、
未启用sparse KV offload。铅笔算术题返回正确答案6，`finish_reason=stop`，
completion_tokens=507，其中reasoning_tokens=391，content与reasoning_content分开。
这是用户提供的模型加载/短生成初步证据，agent未在NPU独立执行。
响应中的model字段仍为`GLM-5.1-w4a8`，不能以此字段确认实际加载权重；后续归档部署SHA、
模型路径/config和完整量化描述。此短请求不覆盖>2048的真实稀疏选择，也不替代普通
sparse PD与mempool对照，验收项保持未勾选。

2026-10-11用户确认join问题已解决，TransferEngine解析失败在当前可访问机器未复现，
两项按[独立记录](../startup-investigation-2026-10-11.md)保存，不继续作为本票的启动阻塞。
下一会话由[续接入口](../SESSION-RESUME.md)恢复，先审计已有算法接线与缺口。

### 2026-10-07：用户要求优先创建GLM-5.2算法适配票

用户提供GLM-5.2模型配置及量化描述片段，随后明确要求创建ticket10。
本轮建立任务、算法合同、实施顺序及硬件验收条件；03已关闭，10可开始，04等待10。
目前只完成已有代码入口核对，未实施本票、未执行GLM-5.2模型或NPU验收。
用户尚未反馈普通GLM-5.2 sparse PD是否已跑通；该事实在baseline阶段核实。

### 2026-10-11：紧凑 Index K 实现交付，等待用户执行 NPU 验收

本轮基线为 `cryang/dev/mempool` 的 `8717142d31`。已有计算路径继续按显式
`indexer_types` 产生/复用 top-k；新增策略仅开放受支持的 Ascend BF16 sparse PD
（普通及正式 mempool，PP/CP/DCP=1、主模型非投机、无 HiCache/HiSparse 等缓存扩展），
把实际 producer 层集合同时接入 pool 分配和容量估算。提供配置选出21层，
GLM-5.1 全 full 配置保留78层。非法 indexer_types 或首层无 producer 明确拒绝。

普通 sparse PD 发布 P 的实际层号，拒绝同数量但 producer 不同的传输；正式 mempool
继续使用已有 peer 布局准入及 Index K-only handoff。NPU shared 层缺少本 forward
producer 时提前报错。资源报告分别记录78个 model layers 和21个 indexer layer IDs。
全部78层 compact KV 的 BM writer/fetch、cache 与完成检查继续使用模型层数。
P 在写入完成后发布 KV_READY，Index K/aux 仍走 native handoff；D 等两类就绪后运行，
drain 和 DONE/RELEASE_ACK 后才能归还 slot，Index K 紧凑化不改变这些所有权条件。

必要的通用文件修改有三处：`mem_cache/kv_cache_configurator.py` 的 NPU pool 工厂选择
producer 集合；`model_executor/pool_configurator.py` 的 NPU 成本分支与实际分配一致；
`disaggregation/utils.py` 为 Ascend sparse PD 发布真实源层号。布局策略本身放在
NPU `sparsity_driven_kv_offload/config.py`，通用 `_should_elide_dsa_index_k()` 的
非PD限制保持原样，其他后端未开放紧凑PD。

新增/扩展的独立 gate 覆盖21层布局/映射/字节数、普通与正式传输、同数量布局拒绝、
78层独立KV数值/完成/重绑，以及资源日志与输出token校验。
[GLM52 NPU说明](../../../ascend-mempool-test/GLM52.md)提供组件gate、3072-token输入容量、
四组普通/正式 eager/Graph启动、固定chat输入、逐token比对、GLM-5.1回归及回传清单。
算术 sanity 输入不含答案，checker拒绝截断、被裁剪的prompt和42.5等错误答案。

Agent 在 Mac 实际执行：独立 CPU 全量223项通过；审查整改后 token gate 的4项定向测试
通过；变更文件 Ruff、format、按目录配置的isort、Python3.9语法解析、bash -n、
服务启动dry-run及git diff --check通过。mypy2.4检查29个相关源文件通过；全目录检查
另发现未修改的 `scripts/verify_resources.py:243` 有 NumPy `pages` 变量的
`var-annotated` 报错，因此该文件从通过的29项范围排除。mypy2.4不支持目标Python3.9，
本次仅在临时配置切换至3.12运行类型检查，仓库配置未改。
SGLang注册单测在Mac因缺少triton无法收集；未执行NPU、真实模型或远端BM测试。

Standards/Spec两路review完成：交付成功标记及算术sanity的发现已修复并复核，
无未解决的代码/交付阻塞。仍待用户代码核对、完整W8A8量化与实际加载、真实21→57
复用链、2048边界/长请求、NPU eager/Graph数值、服务生命周期、token对照和GLM-5.1
硬件回归。按verification.md保持 `State: open`、验收项未勾选，04尚未解锁。

### 2026-10-11：容器内自动验收入口交付

用户反馈正式服务已正常启动、小问题回答正常，并回传BM初始化日志：78层、
16个request slots、每slot 4096 tokens、max_context_len8192。这是用户提供的短请求
证据，agent未执行NPU测试；slots不表示模型层数，容量观测不等于百万token上下文验收。
尚缺完整组件/模型对照报告，故不据此勾选其余验收项或关闭ticket。

应用户要求新增[容器自动验收说明](../../../ascend-mempool-test/TICKET10_DOCKER.md)，
入口为 `ascend-mempool-test/scripts/run_ticket10.sh` / `run_ticket10.py`。
P/D现有容器各启动一次，通过固定阶段控制通道协调，不需要SSH。
默认执行21层Index K transfer、78层独立KV/Graph数值组件，以及同一GLM-5.2的
native/formal × eager/Graph四组服务；复用已有10请求fixture、逐token对照和正式服务
生命周期checker。可选追加GLM-5.1同矩阵，未运行时报告明确写`not run`。
运行前核对两侧SHA、依赖版本、模型/tokenizer元数据；日志与报告保存到
`/home/cryang/log/ticket10/<run-id>/<role>/`，禁止覆盖已有run。
子进程使用独立进程组，阶段失败只清理本入口拥有的进程；进程检查失败也回收子进程。
`run_service.sh`默认日志同步移至`/home/cryang/log/`，其他服务参数未改。
本次修改均为独立验收工具、测试和文档，没有修改SGLang通用生产模块或NPU运行时。

Agent在Mac实际执行：独立CPU全量231项通过（新增8项），覆盖双端四组编排、
真实日志checker/报告对照、错误传播、分帧日志传输和清理异常；模型可执行进程由fixture
替换，不能作为NPU通过证据。Ruff、format、isort、Python3.9语法检查、bash -n、
无NPU的`--describe`及git diff --check通过。临时Python3.12配置下mypy检查30个源文件
通过，沿用前次对未修改`verify_resources.py`的排除。Mac沙箱阻止ps的初次测试失败；
修复清理异常路径后在允许ps的执行环境重跑，上述结果为最终实际结果。
Standards/Spec审查发现的mini-lb健康路由和未执行回归误标均已修复、补测并复核。

等待用户执行NPU验收。`automated_checks_passed`仅表示所选自动项通过；仍需用户
核对baseline文本、完整量化/实际后端、性能观测及GLM-5.1回归证据，再确认关闭10。

### 2026-10-11：用户回传 glm52-r2 双机自动验收通过

用户在npu1-31（P，10.120.72.31）和npu1-32（D，10.120.72.32）的现有容器内，
从`/home/cryang/sglang`运行`run_ticket10.sh`，run-id为`glm52-r2`，模型路径为
`/data_lib/data/models/GLM-5.2-w4a8`。两侧回传的完整控制台阶段结果均为：
transfer、fetch、native-eager、native-graph、formal-eager、formal-graph、
glm52/token-comparison通过，最终输出`AUTOMATED_CHECKS_PASSED`。
P还显示三组compare进程均成功完成。

这是用户实际NPU执行证据，不是agent的CPU模拟测试。按交付入口的检查语义，覆盖
21层Index K传输、78层独立KV数值/Graph组件、四组真实模型请求、正式生命周期checker，
以及其他三组相对native-eager逐token一致性。固定输入包含1024、2047、2048、3072
prompt tokens，另有zero-decode，两轮共10请求；请求checker包含算术sanity。

日志根目录：`/home/cryang/log/ticket10/glm52-r2/`，两侧报告分别为
`prefill/summary.json`和`decode/summary.json`。交付提交为`4dc8607ef3`，实机精确SHA、
依赖及模型摘要仍以待回传的identity/summary为准；目前未读取详细JSON、模型响应或rank日志。
本次命令未传`--glm51-model-path`，因此GLM-5.1回归尚未执行。
下一步归档详细报告，核对部署/完整量化与性能观测，补GLM-5.1回归后由用户确认关闭。
保留State: open；不把GLM-5.2已通过的自动项继续记为尚未运行。


### 2026-10-11：详细报告核对，GLM-5.1回归改为快速冒烟

用户补齐P/D summary与formal eager/Graph service.json。两侧SHA均为
`4dc8607ef3ffa7353dadc68c04a7f490a2b6345b`；依赖均为torch `2.10.0+cpu`、
torch-npu `2.10.0`、sgl-kernel-npu `2026.9.0`、memfabric-hybrid `1.1.4`。
模型config、quant描述、tokenizer及chat模板摘要两侧一致。
两组formal报告均为formal_service_passed，每侧16 ranks、10请求，其中2个zero-decode、
8个decode。所有P/D rank均为model_layers=78、registered_index_k_entries=21，
producer层号与提供配置完全一致；index_k_bytes=44728320。
registered_main_kv_entries、host_kv_bytes、staging_bytes均为0，transport_staging=false；
P native_kv_bytes=747601920，D native_kv_bytes=0、sparse_cache_bytes=3128426496。
每rank本轮累计Index K传输99090432 bytes、aux 97920 bytes，两组一致。
这些累计传输量不能与pool常驻分配字节数直接比较。

service.json的accuracy/performance pending是独立生命周期checker的固定字段，
不覆盖summary中已完成的算术sanity及逐token一致性结果；性能仍未测量。
用户要求GLM-5.1“快速的就可以，不用那么复杂”，后续回归范围据此改为正式mempool +
Graph的短请求冒烟：确认加载成功、78层Index K、连续不同问题正常回答且无错误。
不要求重跑GLM-5.2或GLM-5.1四组矩阵；该快速回归不宣称普通/正式逐token对照已完成。
当前仍等待该冒烟结果及用户最终验收确认。


### 2026-10-12：GLM-5.1快速自主回归入口

按用户要求扩展现有run_ticket10入口，增加`--suite glm51-quick`。P/D各运行一次，
自动核对版本/模型元数据、启动GLM-5.1正式mempool + Graph及router，发送三道短题
各两轮和两个zero-decode（共8请求），检查答案42/63/84、78层Index K及KV、Graph、
全rank释放/slot复用，然后清理自有进程。默认权重路径为GLM-5.1-w4a8，context1024、
P/D容量512，日志仍在/home/cryang/log/ticket10/<run-id>/<role>。
成功报告标记glm51_regression=quick_smoke_passed，不运行GLM-5.2、组件或四组对照。
根AGENTS.md新增用户要求的自主测试约定。仅改独立验收工具/测试/文档，未改生产运行时。

Mac实际执行：独立CPU全量234项通过；双端快速编排使用替代模型进程，保留真实
生命周期checker及控制通信，未运行NPU。Ruff/format/isort、Python3.9语法、bash -n、
--describe、git diff --check通过；沿用临时3.12配置及verify_resources.py排除的mypy
30源文件通过。Standards/Spec审查无阻断问题；续接文档已改为运行GLM-5.1快速入口，
避免重跑已通过的GLM-5.2。GLM-5.1实机结果待用户回传，ticket保持open。
