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
- [ ] 同版本GLM-5.1原有配置、普通/正式模式回归通过；记录GLM-5.2性能观测，
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

### 2026-10-07：用户要求优先创建GLM-5.2算法适配票

用户提供GLM-5.2模型配置及量化描述片段，随后明确要求创建ticket10。
本轮建立任务、算法合同、实施顺序及硬件验收条件；03已关闭，10可开始，04等待10。
目前只完成已有代码入口核对，未实施本票、未执行GLM-5.2模型或NPU验收。
用户尚未反馈普通GLM-5.2 sparse PD是否已跑通；该事实在baseline阶段核实。
