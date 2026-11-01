# 12: K1-05d 主存五类区域改 page first（布局正确性，行为不变）

**What to build:** 在票 08 验收后的 8 / 8 / 1 / 1 / 1 布局上，把五类主存区域（P alive 主 KV、D alive 主 KV、P alive Index K、prefix 主 KV 8 段、prefix Index K 8 段）从 layer-first 改为 page first：区域 = [物理页, 层, 页内 token, head, feature]（C32），主 KV 与 Index K 分区、同一逻辑页对应两个连续块（主 KV M = 11,501,568 B、Index K I = 688,128 B）；alive 物理页 = 槽 × (S ÷ 128) + 槽内页，prefix 物理页 = 段内页；地址 = 区域基址 + 物理页 × page_bytes + 层 × 128 × 行宽 + 页内 token × 行宽 + 行内偏移。只保留 page first（不设生产开关），布局类型、B、各维度进注册 / describe 与启动签名；重启建新池、不做在线迁移。第一步先验证现有按 token 行索引的拷贝接口对 page first 行集与超过 4 GiB 跨度的支持（默认按槽 / 段给基址使单次跨度 < 4 GiB、不改拷贝内核；不够再最小扩展为 64 位偏移），再改布局层、会话与视图、写回与稀疏读、prefix 三操作、注册；仍用逐层索引拷贝，给出整页块接口供票 11 接入。HBM 原生布局、副本数、页归属、分片分工、prefix 分配规则与描述符编码、整槽后端、串行、控制协议与完成语义、main router 消息都不变；不做执行器线程与批量接口（票 11）、远端 prefix、动态分配、并发、按层流水、性能埋点。细节见 k1-tickets.md 第 16 节。

**Blocked by:** [08（K1-05b prefix 单份：8 段、numa interleave、按段读者、并行 admit、直接 fill）](08-k1-05b-prefix-single-copy-numa-interleave.md)（已用户验收关闭）；阻塞11，09经11传递依赖12。

**Status:** ready-for-agent

**State:** open

- [ ] 第 1 步证据：拷贝接口对 page first 行集与 > 4 GiB 跨度的验证结果归档，写明采用分段基址还是最小扩展，以及实际拷贝接口如何支持
- [ ] CPU 几何与寻址：跨层、跨页 127 / 128、跨槽、最后槽、跨段；两类数据；区域与探针不重叠；页块接口与行公式一致（任一页的所有行落在该页块内、相邻页块首尾相接）
- [ ] 独立 CPU oracle：用可辨认的 region / slot / page / layer / token / feature 值验证；写读双方不共用待测公式；已有 fake copy 随新契约改，不只改预期数字
- [ ] 写回与稀疏读：P 分写、Index K 单写、D 特征分写；prompt / decode 边界；当前 token 本地供给；未写行与 padding 由 mask 排除
- [ ] prefix：31 / 32 页、跨段、零页读者、每页单写者；不连续原生目标页；fill 的 8 份主 KV + 1 份 Index K 展开
- [ ] 图重放：稳定输入地址、换槽、零有效行、逐步写读顺序；设备端无新增主机同步或每步主机读回
- [ ] 端到端：复用 08 的两轮六例入口，内容、答案、ready / free、复用、超时、重启、清理全过；字节计数与 08 相同（B：主 KV load_back 368,050,176 B、fill 2,944,401,408 B、Index K 各 22,020,096 B；C 31 页时各段读取量不相等不是错误）
- [ ] 布局证据：注册 / describe 体现 page first、B 与各维度；启动签名含布局类型；精确公式与端到端结果同时成立
- [ ] 证据边界沿用 08：不新增直接主存读回；物理 NUMA 驻留、性能、直接主存内容比较不在本票；不出基线表、不承诺性能收益
- [ ] 最多 2 轮、复用服务启动；实机由用户运行，失败返回非零，CPU 通过不代替实机；单 commit；Standards / Spec 两轴评审；用户确认实机验收后关闭并解锁 11

## Comments

### 2026-11-02：同步page first改序定稿

按用户交接导入syncfile定稿第16节。08已用户验收关闭，前置已满足；本次仅迁入设计与交接记录，未开始实现、未运行接口实验或NPU验收。第一步先验证小几何及真实P几何的行索引与宽地址；归档证据后再推进布局改造。设计侧负责架构逻辑，Ascend/NPU具体接口、内核与图捕获方案由实施侧核对实际代码和实验决定。State=open；用户确认本票NPU验收后才关闭并解锁11。

### 2026-11-02：开工，第1步接口证据与布局适配

用户显式调用implement授权实施。测试边界沿用本票已批准的布局/页块、拷贝、运行时读写、prefix与签名接口；采用独立CPU oracle。
[首份证据](../verification/ticket12-copy-contract/README.md)归档CPU小几何、真实P宽地址及实际内核源码约束。
选择固定分段基址配合设备mask，保留UniDexCopy；NPU能力仍待用户实测。
布局、页块view及读写适配代码正在实现；两项新布局测试先因缺少page_size接口失败，完整回归待完成适配后集中运行。
State=open，未关闭或解锁11；工作区既有文档改动保留，评审基点为开工HEAD b4a925a3a3。

### 2026-11-02：实现、CPU检查与双轴评审交付

五类区域、页块接口、写回/稀疏读/prefix寻址、注册和启动签名已接通；默认行为、HBM与完成语义保留。
仍用UniDexCopy：大区域按槽，超过4 GiB的旧组件槽按完整页进一步分段；索引和mask留设备，图中固定调用基址。
[交付说明](../delivery-ticket12.md)列源码阅读顺序；[NPU入口](../../../ascend-mempool-test/K1_TICKET12.md)自动协调双机两轮六例，并在模型前验证独立设备内存宽地址/Graph。
没有新增BM主存读回、性能埋点或11的执行器。

[CPU记录](../verification/ticket12-cpu-20261102/README.md)：完整539项一次，定向37项；新增2个方法后合并541项中540项验证通过。
旧worker注册方法两轮均因夹具适配失败，最后改为重建不可变PoolLayout，经只读评审确认但按两轮上限未再次运行，不计为通过。
37文件静态、21模块strict mypy通过；[Standards/Spec](../verification/ticket12-cpu-20261102/review.md)均0项需修改问题。
NPU未运行，验收项暂不勾选，State=open；用户确认本票NPU验收后才能关闭12并解锁11。

### 2026-11-02：NPU002在首轮清理后失败

用户回传95cac11ee3的32报告：page-first-copy/fetch通过，已执行超时注入及双端进程清理；随后端口复用检查报EADDRINUSE，具体端口待定位。退出日志另有memfabric析构崩溃。
[反馈与边界](../verification/k1-ticket12-002/README.md)保留证据来源；未通过端到端及restart，State=open，11仍阻塞。未修改实现或安排重跑。

### 2026-11-02：修复002端口检查误报

用户定位26088为到4.190.51.35:443的ESTABLISHED连接；该端口不属于D的16个实际BM端点。
旧脚本扫描整个512端口预留区，已改为复用SharingPlan实际NIC/store清单，启动前/清理后共用；绑定失败包含地址。
仅改验收脚本，生产源码与默认清理不变。新增回归先失败后通过，定向12项通过，静态通过。
Standards为0硬违规/1非阻塞建议，Spec为0问题。修复后NPU未运行，原始002失败及memfabric析构异常保留，12仍open。
