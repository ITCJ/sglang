# K2 当前记忆

**2026-11-05：14已实现，等待NPU验收。**
入口`ascend-mempool-test/scripts/run_bandwidth.sh`；默认remote-write，先跑31/P与32/D。
8K及原三档、单/双写、D读干扰基线、连续逐层、完整读矩阵、Mooncake探测与报告已交付。
新增6项CPU验证通过，6项原有失败在基点复现；两轴评审最终0未解决。
运行说明见[14交付](ticket14-delivery.md)。14保持open；25没有实机前置证据，尚未解锁。



当前：13按007单组约定暂时验收完成。18代码与CPU检查、两轴评审、自运行入口已交付，等待用户NPU验收；18保持open，不解锁25/19。

先读[18交付](ticket18-delivery.md)。用户确认64位kernel已在Docker装载且Python接口不变，本地只改adapter。
协议slot继续作为绑定身份，存储分配已改页集合；trace默认关闭。

001在target验收器因旧控制长度合同失败，已修复并通过4项CPU回执检查；下一轮仅b64-sd512、run-id=k2-ticket18-002。

## 本轮教训（后续执行）

- 观测与trace分开：trace默认关，只按需采少量step；全程TP16采集产生GB级文件，同步解析阻塞tick并触发心跳超时。先收窄采集，勿先加超时或导出子系统。
- 测试覆盖真实入口：空warmup/replay的索引显式int64；离线容量重放关闭服务观测副作用。Graph小测试通过不代表完整工作流通过。
- 排错先找首个致命异常：137和SDK退出析构崩溃可能是后果；MAPPING_PENDING的ERROR可重试恢复。结合时间线、ready和完整异常栈判断。
- 兼容部署版本：CANN9.0 trace的lane、时间戳及跨replay合并不能按理想格式解析；先用实机原始证据验证。
- 沿用已接受的模型精度边界：已知17→12问题保留诊断，目标与本轮原生参考完整token ID比较；勿重复设置同一阻断。
- 控制验收成本：先单组定向验证，减少重复模型加载；尊重用户接受范围，不追加补测提醒。每次同时给31/32完整命令。
- 记忆与报告须准确：页大小相关说明从实际配置生成；旧记录标为历史，顶部保持最新状态，CPU通过与NPU验收分开。

## 历史过程（以下旧状态已由顶部当前结论覆盖）

- 003 的fork输出与K1-07旧问题一致；用户再次明确是已排查的量化模型精度问题。
  本轮纠正验收脚本：仅该fork以目标/当前原生参考完整ID一致为门槛，严格answer failed原样保存。
  原6例和off/on不变；13项定向测试通过。见[ticket13 fork修复](ticket13-fork-baseline-fix.md)。
  002曾因6699占用失败，之后端口已释放；003已越过该问题。下一次用004，13仍open。

- 用户已回传31日志复核passed：两轮forward各1条、missing=[]。
  下一步31/32完整验收使用k2-ticket13-002；13仍open。

- 用户明确继续CANN9.0.0，仅修日志。原始index5–8各四个打点可恢复两轮；
  自动离线配对并用捕获流映射区分主侧流，原实测包离线通过，18项测试通过。
  见[日志补丁](ticket13-cann90-log-fix.md)。下方首次反馈限制已由本条更新。
- 31首测页计数/快照通过、服务未启动；Stream lane误过滤及字符串时间戳已修复。
  15项观测测试通过。新增 `--analyze-only`；旧trace仍有跨replay层区间，不能宣称Graph计时通过。
  见[修复交付](ticket13-trace-fix.md)，原NPU采集不重跑、不改profiler调度。
- 当前以[13交付](ticket13-delivery.md)及[检查记录](verification/ticket13-20261103/checks.json)为准。
  默认关采集、独立快照、Main/SR/SDK因果、MSTX设备范围、四项分析和双机四组off/on已接线。
  两轮首轮预热n=1，同服务追加fork；默认父进程退出不变。
- 一次完整CPU563方法，受影响项第二轮23项全过；最终567不同方法561通过，6既有基线失败。
  19文件静态、16源严格类型、双侧describe通过；Standards/Spec复核0未解决。
- Graph、真实模型计时/字节/开销和用户三档缓存日志未验收。13 needs-info/open，不解锁18/21/22。
  TTFT按Main感知D首输出就绪实现，包含观测消息延迟；未称用户此前单独确认。
- 下方是本次交付前的规划/同步记录，代码状态由上方更新覆盖。

- 13 开工读 [实施方案](ticket13-implementation-plan.md)，从 G 节推进；本轮只改资料。
  主线预计仅 `scheduler.py` 增加结果完成通知，理由和行为影响已提前说明。
  默认关闭；采集、文件 writer 与离线分析复用既有完成路径；Graph 计时先验实际 replay/task 关联。
  上游 NPU Event 读取可能同步，不能套 CUDA external 参数或用 Python 提交时间代替设备耗时。
- 13 新核对：本地缓存容量仍等于模型 top-k，slot map 每步重建，不是已实现的独立 2k LRU。
  记录真实 hot_cache_tokens 与 replace_selected；用户三档分支日志独立接入，缓存策略留22。
  单请求可有 scheduler overlap，T_gap 使用有符号差，并单列 gap/overlap。
- TTFT 已发起可选澄清，当前提案用 Main 感知首 token 就绪，含观测消息延迟。
  未收到用户回复，不记为已确认；后续回复优先。完整验收保留09四组及开关两态，不能缩为单组smoke。

- 阅读入口：[spec](spec.md) → [执行文本](k2-tickets.md) → 对应 issues。
  [四项答复](handoff-check-20261103.md)记录源码依据和建议，未当作硬件结论。
- K1 10 张功能票已验收关闭；04→K2 14、10→K2 13，按本次交接转归关闭。
  K1 12 closed（10 验收、2 转归），K2 13 open。
- 主线 13→18→25→20→15→16→17→24；当前可开始 13、14。
  14 不经服务，先测 P→D 4K/32K/128K 远端写（有/无 D 本地读）。
  25 还需 18 关闭、远端写可隐藏的结果、多基址 kernel 可用。
- 论文范围收窄为 sparse attention + KV offloading，强调池与调度。
  冻结项、保留功能与策略开关规则见 spec，不沿旧“下一步阶段 1”推进。
- 代码基点 `dc31b37cbe` 已完成 K1 验收后清理；用户回传仅证明单配置精简回归。
  13 做残留核查与观测模式，不恢复人工延迟；保留既有证据边界。
- 用户并行测 TP16/单请求、热缓存容量 k/2k/4k（k=2048）。
  日志需逐页 hit/miss、源区域、读者机器/组/NUMA及 producer 选择一致性。
  用 page_size_tokens 与 hot_cache_tokens 区分两种 B；13 离线直接读取。
- 用户报告单基址 UNIDEX 已改 64 位；本地 adapter 与旁侧 kernel 仍旧。
  18 接入已交付版本后改为单基址 64 位，不等多基址；多基址留 25 跨区域读。
- 现有 manager 已映射 D alive；MempoolKVOffload 仍拒绝写另一侧。
  库有 L2G/batch/wait，当前 BMTransfer 只封装 G2G。
  25 首版建议库完成；BM wait 不覆盖绕过库的 UNIDEX 远端写。
- 设备页表拟复用固定 Tensor、图外 copy_ 和 _binding_update 事件排序。
  18 页表版本负测试；15 后并入代号。未通过新页表图重放 NPU 验收。
- 已有默认：15 FIFO；16 每组 SR；17 miss 估计先常数；20 chunk=8192；25 结束归还 D 副本。
- 21 先跑一次未修改 HiSparse 同机对照；22 复用，缺 B 档才补。
  19/23 等第 1/2 组数据及 25 结果；19 开票判据、24 SLO 待用户。
- 源稿冲突（15 单 chunk、25 远端命中/Index K 字节等）集中见 spec，原文快照不悄改。
- 实机由用户运行；每票 NPU 确认后关闭。每票单 commit、两轴评审、自运行验收、最多两轮。
  默认退出沿父进程清理；本轮没有硬件访问、代码提交或 syncfile 写回。

**2026-11-04 空请求观测修复与暂时验收约定。**

004 的 b128-sd128-off 完整通过；on 在 decode warmup 因空 rows 默认 float 索引失败。
修复 runtime 快照索引为 int64；空 replay 回归先复现后通过，观测/runtime 32 项 CPU 测试通过。
新增 --geometry b64-sd128 --observe on，只执行指定服务组合，保留 Graph gate 和该组完整功能检查。
用户明确约定：下一轮该组通过即暂时接受13，然后推进18；其余测试由用户自行安排，不提醒。
当前修复已实现，所选 NPU 组尚待结果；未提前宣布13通过或开始18实现。

**2026-11-04 005 离线容量重放修复。**

005 Graph 通过，restart 后离线 capacity_check 使用 observe=True 创建 MainRouter，触发服务包导入错误。
仅为离线准入重放复制 observe=False 配置；真实服务继续 observe on，容量不足及账本检查保留。
回归先复现导入错误；修复后该回归与原容量重放检查通过。
整份 session 测试运行16项，存在7个旧端口断言失败及1个沙箱 bind 权限错误，未宣称全通过。
下一次仍只运行 b64-sd128-on，使用新 run-id 006。暂时验收及随后推进18的约定不变。

**2026-11-04 用户调整：默认关闭 trace。**

006 在同步 profiler 解析期间停止 SR 心跳，600秒后 fail-stop；单个未完成 trace 超过1.2GB。
用户要求停止自动 trace 采集，后续按需手动采少量 step。
新增 observe_trace=False；普通 observe on 保留 JSONL、页统计、字节及生命周期验证。
默认跳过 Graph trace gate；报告标记 device_timing=not-collected，未将设备计时视为已通过。
撤回未提交的自动离线导出方案。48项CPU测试通过；Standards/Spec评审无阻断项。
下一轮007仍为 b64-sd128-on，trace关闭；此组通过即按用户约定暂时接受13并推进18。

**007 用户实机证据：13按约定暂时验收通过，解锁18。**

用户回传31/32的 report 与31的 v1-v10：两端 passed_current_configuration，
b64-sd128-on，trace_enabled=false，阶段 exit_code=0。
V1–V10、长生成、timeout_fail_stop、paired_cleanup 全 passed；六例完整输出ID与参考一致。
两端 observation_status=passed、missing=[]；B/C load_back/fill 四项字节核对全通过。
日志根目录 /home/cryang/log/k2/k2-ticket13-007；交付版本4864e534a7，SDK mf114-c01f3ad-ticket11-batchfix。
按用户预先授权“此组过了就算暂时验收通过，然后推进18”，13按此范围关闭、18解除依赖。
原始报告 user_accepted=false 是运行器固定字段，人工结论在本记录补充，不修改原始证据。
范围为单组JSONL观测与功能回归；本轮未采设备trace或比较off/on开销。
原清单未获本轮证据的条目保留未勾选，不代表仍阻塞18；不追加补测提醒。
报告V1/V6/V7的detail沿用128页大小静态文案；本轮实际page_size=64，C命中4032、重算64，
prefix页数及字节以k1.shared.writes/layout实际证据为准。
