# 03: 策略选择与同负载性能验收

**What to build:** 基于前两阶段读写对照和真实服务并行路径，选择 MLA NUMA 共享
的写入粒度与调度。固定同一 NUMA 分配，以share=0的16份独立池为基线，证明
默认每节点两 ranks、share=1的8份共享池的去重收益，并使
同负载 decode 吞吐下降、ITL 中位数/P95 上升分别不超过3%。

**Parent:** [MLA NUMA 共享 spec](../spec.md)

**Blocked by:** [02: MLA 共享 KV 与写入并行](02-mla-shared-kv-overlap.md)
的用户 NPU 验收与关闭。

**Status:** ready-for-agent

**State:** open

## Scope and decisions

- TP16、BF16 MLA，P/D 相同逻辑分组，NUMA 各自配置；GLM-5.2 主验收，
  保留 GLM-5.1 快速兼容检查。复用01组件和02服务入口，避免重新建测试体系。
- 主验收固定每节点两个 ranks，分别重启运行 share=0 和 share=1；NPU、CPU
  亲和性、NUMA 分配与逻辑容量均相同。组件 TP1/2/4 证据复用；四成员共享的
  容量/带宽取舍单列，不将其当成双成员默认部署必须追加的全量服务矩阵。
- NUMA 分配变化单独成组：固定共享开关后比较分配配置。只在改动影响该路径
  或需要解释服务瓶颈时补测，不把已通过的493项旧组件矩阵作为默认前置重跑。
- token/block、layer、feature 是可行性与性能候选，非必须全部成为生产模式。
  每步单 token 的分工、producer/shared-topk 层和小/大 batch 分别比较。
- 把布局/分工收益与 stream 调度收益分开，再对最终方案作端到端验收。
  [执行设计](../execution-design.md)规定读写口径、DAG 与比较方法。
- 分别固定变量比较 selected current-fill／历史 copy 的并行收益，以及
  同层写回 join／跨一层写回的收益，包含尾部 flush 和实际 staging 开销。
  写回不跨 decode step，最终同负载指标包含所有必要完成依赖。
- 不以容量扩大掩盖同负载回退；首版不要求填满3M或2T，也不以各NUMA容量
  上限调查阻塞关闭。Index K HBM 不属于本次去重。

## Acceptance criteria

- [ ] 归档候选写入粒度和调度的实测结果及选择依据。所有候选写入无重叠/缺失，
  current 本地供给、历史发布、Graph 和 drain 合同成立。
- [ ] 固定权重、量化、输入/输出长度、并发、Graph、逻辑 KV 容量、NPU/NUMA 与
  软件环境，对照同一 rank→NUMA 分配的 share=0/1；记录所有运行参数与版本。
  分组变化、NUMA 分配变化、调度变化分别对照，不能同时改变后归因于共享。
- [ ] 覆盖单/多请求、短序列与大于 index_topk 的长历史，报告实际 hit/miss；
  各代表负载暖机后交替多轮比较，保留原始请求/流式 token 时间，ITL 不用TPOT代替。
- [ ] 每个代表负载分别满足 throughput_shared ≥ 0.97×baseline、
  ITL_P50_shared ≤ 1.03×baseline、ITL_P95_shared ≤ 1.03×baseline。
  噪声影响判定时追加成对样本，不用跨负载平均隐藏回退。
- [ ] 默认部署报告16→8份 payload，自定义部署按实际组数报告；列出各participant
  实际物理贡献、请求 NUMA、物理落点证据及对齐/probe开销。无法观测的页落点
  保留unverified；不能按带宽反推已验证。写入量、完整读取量、copy有效带宽、
  服务性能分别统计，不能声称读取量减半。
- [ ] 性能问题回到候选或并行调度修正并重测，不能因去重成功或正确生成文本
  就关闭。profile与最终计时分开；最终默认策略依据结果确定。
- [ ] 自运行入口完成服务准备、请求、校验、对照报告及自身进程清理，多机每机
  一条命令；`/home/cryang/log/`保存原始数据与报告，失败返回非零。
  smoke 与完整性能验收清晰区分。
- [ ] 汇总已完成/新增测试及复用来源，保留原始样本、P50/P95、阶段进度和耗时。
  最终报告明确默认双成员布局、共享开关、writer/调度策略与独立池回退方法。
- [ ] 按[阶段交付流程](../../ascend-mempool/verification.md)完成用户NPU验收，
  在Comments中记录环境、代码版本、真实命令、日志、结果和用户确认。
  满足后才解锁[原ticket04](../../ascend-mempool/issues/04-single-request-graph-decode.md)。

## Verification

复用01已证明的copy口径和02已证明的服务正确性；策略变化后执行受影响回归，
并完成上述多轮同负载对照。容量可分配上限另立实测记录，不将理论预算或VA
映射大小当作实测容量。NPU由用户运行，Mac检查不能替代最终性能证据。

## Comments

2026-10-12：用户确认第三阶段无异议，按既有3%门槛建立。尚未开始，等待02
硬件验收；第一阶段的读写对照与第二阶段的并行设计不延期到本票。

2026-10-14：01由用户接受关闭，02尚未实施，本票继续等待02用户硬件验收。
按用户默认双 rank/NUMA 的决定，主比较固定NUMA分配只切换共享开关。
更改分配或四成员共享单列诊断，既有组件数据不重新混为真实服务性能。
