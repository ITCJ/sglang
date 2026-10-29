# 03: K1-04 prefix split_read（16 份布局）

**What to build:** B、C 命中时，load_back 不再由每个 rank 读整段，而是每个 rank 只从自己那份 prefix 读分到的页，直接读进每层复用的一块汇总缓冲里属于本 rank 的那一段，按层做 in-place all_gather 成全集，再按一张槽位表一次散开到各自的 HBM 页，主 KV 顺手拆回 512 维和 64 维两个缓冲，Index K 用同一张表、另一块更小的缓冲；没有独立暂存区，没有重排缓冲。槽位表每 rank、每次恢复建一次，主 KV 与 Index K 共用，补齐槽位不生成行。输出与参考一致。读者到页的分配是一个可配置的函数，默认轮转，页数不整除时补齐；集合通信只在有序调度 tick 里发起；协调者在所有 rank 散开完之后才对 main router ack；extend 仍等整段完成。副本数不变，op1、op3 不变。细节见 k1-tickets.md 第 7 节（2026-10-30 改为合并缓冲做法）。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

**State:** open

- [ ] B、C 命中用例通过 V1 / V3 / V6 / V7
- [ ] V2 改为 all_gather 后的汇总缓冲和散开后的 HBM 页都与本 rank 私有副本逐 token 一致，主 KV 和 Index K 都比
- [ ] 每 rank 读取字节计数：每页恰好被一个 rank 读一次，总和等于命中页字节
- [ ] 恢复路径每层每类只有 H2D、all_gather、散开三步；HBM 临时缓冲只有两块汇总缓冲，大小等于槽位数乘页字节，首次恢复打一行日志
- [ ] 槽位表每次恢复只建一次，主 KV 与 Index K 共用；4K 配置下 rank 15 的补齐槽位被跳过
- [ ] op2 的 ack 时刻晚于最后一个 rank 散开完成
- [ ] 把分配函数换成另一种（例如连续块）结果不变，只有槽位表变、三步不变
- [ ] 基线表并列，重点看 T_lb_rank 与 op2，op2 拆成 H2D、all_gather、散开、控制四段；不设门槛，变差要解释

## Comments

### 2026-10-30：合并缓冲版本交付

两块汇总buffer、共享slot表、in-place all_gather及单核散开完成。
真实核地址算式的CPU验证覆盖flat/FIA NZ、512/64拆分、Index K与补齐跳过。
V2双重核对、三步/两缓冲证据及四段诊断表已接入两轮自运行验收。
临时计时在工程分支交付，用户验收后删除；baselinetest未修改。
最终491项CPU、10模块strict mypy、静态检查通过，Standards/Spec无剩余发现。
修复首次Triton编译混入scatter计时，实际仍单次发射；旧490项首轮通过记录保留。
详见[最新验证](../verification/ticket03-combined-cpu-20261030/README.md)和[交付](../delivery-ticket03.md)。
NPU未执行，验收项不勾选、State=open。旧四跳交付记录仅作历史保留。

### 2026-10-30：合并缓冲定稿实施

用户要求替换四跳恢复，已同步syncfile第7节与票据03；保留此前历史记录。
用户明确暂不修改baselinetest分支，四段计时和验收放入本次commit。
工程中的临时计时接线在用户确认验收后删除；当前不得提前删除而失去验收入口。
沿用已约定的PrefixStore内容、SR完成与自运行报告三个测试边界。

### 2026-10-30：实现交付，等待NPU验收

轮转/连续分块、补齐及零页读者、逐层all_gather、原生主KV/Index K恢复已实现。
复用SR有序tick与全rank完成回执；私有prefix逐token oracle及生产读量分别计数。
`split-read-acceptance`复用两轮入口，首轮轮转、重启轮连续分块；默认父进程清理。
完整487项CPU通过，9模块strict mypy、14文件静态检查与两轴review通过。
Spec发现并修复截断输出可能漏检；额外1项定向测试验证空/缺失策略输出被拒绝。
完整套件仅两轮（首轮沙箱权限阻断），未跑第三轮。
[交付源码阅读顺序](../delivery-ticket03.md)、[逐机命令与精简范围](../../../ascend-mempool-test/K1_TICKET03.md)、
[本机验证及审查](../verification/ticket03-cpu-20261030/README.md)已保存。
未执行NPU；性能对照在测试分支另行安排。验收项保持未勾选，State=open。

### 2026-10-30：开始实现

用户通过`$implement`授权实施。基点`e23b57df7d`；01、02已验收关闭。
复用PrefixStore恢复、SR有序tick与全rank回执、自运行验收入口三个既有边界。
保持16份布局、默认退出与Ascend/NPU目录范围；性能工具继续留在测试分支。
分工读取的16 rank CPU用例先失败后通过，覆盖两策略、1/31/32页及实际读取量。
功能接线已完成，验收入口和整体检查待完成；NPU未执行，State保持open。

### 2026-10-29：同步 syncfile 定稿

按用户要求导入已定稿的票据。目标、验收项及依赖沿用原文；
本地补充 State 与依赖链接。尚未开始本票实现或 NPU 验收。
