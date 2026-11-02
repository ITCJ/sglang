# Ticket 11：SDMA 最小验证交接（给 high）

日期：2026-11-02。状态：方案草案，demo 尚未编写、CPU/NPU 均未运行。
工程基线：`abdee9d701`；功能版本为已接受的 `c2ed808092`。

## 当前实施口径（取代下方早期草案冲突处）

2026-11-02 用户已授权implement demo。已在当前分支实现共用平台适配层与两组P/D入口。
每侧4个rank，共8参与者；不另做八组demo。后续逐op路径已确定，只复核接口事实与证据。
正常服务及测试服务均不新增目标读回；demo使用真实设备索引读者。
本次交付与命令以 [K1_TICKET11_DEMO.md](../../ascend-mempool-test/K1_TICKET11_DEMO.md) 为准。
实现已完成、本地检查见verification/ticket11-demo-cpu；NPU未运行，11仍open。
下方保留最初方案供追溯，不作为覆盖当前用户决定的实施指令。

## 1 执行边界与交付终点

用户要求本轮 agent 只编写方案。建议采用 ask-matt 的 prototype 路线：
**high 实现独立 demo → 用户运行 NPU → 规划者根据结果定稿传输路径 → 用户核对 → high 实施 ticket 11。**
本文是第一阶段的实施委托草案，需用户确认路线后交给 high。
high 本阶段的终点是 demo 及真实结果的交接，不是生产接入。

使用独立 `prototype/k1-11-sdma` 分支/工作树，保留原型及其证据引用。
当前工作区已有用户修改；先核对 `git status`，保留这些内容。
原型只新增测试工具和说明，不修改生产模块，不自动搬入生产分支。
原型是 ticket 11 的技术验证，不关闭 ticket 11，也不解锁 09。

本阶段只回答一个问题：**在当前安装的 MemFabric 上，SR 分片所属进程能否用自身 NPU 的 SDMA，异步搬运同一 BM 会话内两个 DRAM 区域的连续页块，并可靠报告完成/失败？**

## 2 为什么先验证

按用户指定，以本地 GitCode checkout `../gitcode/memfabric_hybrid` 为唯一 SDK 实现依据。
origin 为 `https://gitcode.com/Ascend/memfabric_hybrid`，VERSION 为 `1.3.0`。
HEAD 为 `29fbd1ae14e7862c2335dc643bdf8084f0253393`（2026-10-08），工作区干净。
GitHub checkout 较旧，不用于本方案的 SDK 接口、缺陷或能力判断。
当前服务创建 BM 时仅选择 `BmDataOpType.SDMA`。Python batch 和 wait 包装释放 GIL。
`flags=1, stream=0` 的普通 G2G 走线程对应的内部 SDMA stream；同线程 `wait()` 等待该流。
这些是源码事实，尚不能证明实机安装版本、全部映射以及新线程上的行为。

关键源码入口：

- [Python batch / GIL](../../../gitcode/memfabric_hybrid/src/smem/csrc/python_wrapper/memfabric_hybrid/pymf_hybrid.cpp:786)。
- [线程对应 stream](../../../gitcode/memfabric_hybrid/src/hybm/csrc/ts_engine/hybm_stream_manager.cpp:25)。
- [SDMA wait](../../../gitcode/memfabric_hybrid/src/hybm/csrc/data_operation/host/hybm_data_op_sdma.cpp:350)。
- [SDMA 任务与所属设备](../../../gitcode/memfabric_hybrid/src/hybm/csrc/ts_engine/hybm_stream.cpp:29)。
- [batch G2G](../../../gitcode/memfabric_hybrid/src/hybm/csrc/data_operation/host/hybm_data_op_sdma.cpp:716)。

**先核对一个已发现的错误传播缺口。** 本地 `BatchCopyG2G` 在子提交失败时设置 `ret`，但末尾返回 `InnerWait(options, 0)`。
异步模式的 `InnerWait` 直接返回成功，因此子提交失败可能没有传给上层。
这不等于已证明用户安装库存在同一问题，也不等于所有正常 batch 都不可信。
但若实机版本仍有此路径，成功的数据 smoke 不能证明失败语义正确。
先记录确切库版本/构建来源；需要 SDK 修复时另向用户说明，不能在 demo 中悄悄替换库或在生产层添加兜底。

## 3 最小设备拓扑与数据

首选单台 P 主机，选两个实际 NUMA 节点，各选同组的两个 NPU worker，共 4 个进程。
第一个节点取实际 NUMA 0，用于模拟单份 alive Index K；另一个取实际远端 NUMA 节点。
设备号来自实机映射，不能直接用 TP rank 充当物理设备号。

原型创建一个 **4 参与者的 BM 会话**，不是四个互不相通的池。
使用直接 BM 初始化的小型测试入口；复用现有初始化/端口/进程管理工具中可用的部分。
不要为了容纳原型而改变生产 `MempoolConfig` 的 TP16 或 P/D 配置限制。

| 测试区域 | 物理分布 | 用途 |
|---|---|---|
| alive main | 每组 1 份，共 2 份 | 验证两个 reader 写同一目标副本的不同页 |
| alive Index K | NUMA 0 的 owner，1 份 | 验证跨 NUMA 的 admit Index K 源 |
| prefix main / Index K | 每组 owner 各贡献一段 | 验证本段 admit、跨段 fill、peer 可见性 |

使用真实页块大小：B=128，M=11,501,568 字节，I=688,128 字节。
先分配少量页（例如 4 页、每段 2 页），不分配模型、原生 KV cache 或生产大槽。
源模式编码数据类型、源副本、页号、层、页内偏移；目标先写不同的哨兵值。
结果期望由测试的独立几何生成，不能从目标倒推，也不能只比较写入和读回共用的地址公式。
同一份 prefix 的规范内容须明确选定：admit main 采用该目标段 owner 组的源副本内容；fill 各副本均应与该 prefix 相同。

原型只证明这个拓扑。进入生产前，再用同一工具在 P 侧 16 个执行设备、8 个 NUMA 段上跑地址路径矩阵，仍不加载模型。
单机 P-only 会话不等于生产 P/D 32 参与者会话；后者在 ticket 11 服务验收中覆盖。

## 4 提交与完成契约

每个模拟 SR 分片只有一个持久 host 执行线程。
线程绑定到该 rank 的 NUMA CPU 集合与容器允许 CPU 的交集，并使用该进程初始化 BM 的 NPU。
固定一个线程服务一个设备；不在线程之间迁移 submit/wait。

一次任务把 main 和 Index K 的页块放入同一张清单：

```text
src_addrs / dst_addrs：同一 BM handle 中的 SDK GVA
sizes：每页的 M 或 I；地址使用 64 位整数
type：G2G；flags：ASYNC_COPY_FLAG（本地源码值为 1，extend 位为 0）
stream：0，使用库的内部线程流
非空清单：一次 copy_data_batch → 检查返回值 → 同线程 wait → 检查返回值
成功后：向结果队列投递完成；主线程轮询队列
空清单：不调用 batch；返回零工作完成记录
```

执行线程可以阻塞在库 wait；控制线程保持可运行。
不要用当前 torch stream 的 Event 代替内部 BM stream 完成。
测试初始化与结果读回可使用 HBM；被测 DRAM→DRAM 路径不使用 HBM staging。
SDK 可将一次 batch 拆成若干 SDMA 任务；“一次 batch”不表示只有一条硬件任务。

## 5 一次运行覆盖的关键检查

| 检查 | 执行与成功判据 |
|---|---|
| 本 NUMA admit main | 每段 owner 用自己的 NPU，从本组 alive 拷入本段；每页目标只写一次 |
| 跨 NUMA admit Index K | 非零 NUMA owner 用自己的 NPU，从 NUMA 0 源拷入本段 |
| 跨段 fill main | 每组两个分片各读分到的逻辑页，写本组共享 alive；同时覆盖非 owner 执行者 |
| fill Index K | NUMA 0 的 owner 从两个 prefix 段拷回单份 alive Index K |
| 页边界/零工作 | 包含非连续页号、混合 M/I 大小和空清单；空清单仍完成协议 |
| 真实目标内容 | wait 后，用独立的 G2L 读回到小 HBM 缓冲并与确定性源比较；再由同组 peer NPU 读目标，证明跨 reader 可见性 |
| GIL 与异步 | 主线程记录 heartbeat；保存 batch 进入/返回、wait 进入/返回区间内的推进证据；窗口太短时结果记不足，不能用线程名证明释放 GIL |
| 完成不提前 | 独立 torch Event 完成不触发 BM receipt；人为延迟结果交还时控制线程继续推进，目标可读回执尚未产生 |
| 错误传播 | fake adapter 的 submit/wait 失败必须使任务失败；核对实机 SDK 的子提交错误路径。仅测非法参数被拒绝，不能覆盖上述 SDK 缺口 |
| 退出 | 正常先 drain 再停止原型线程；失败由测试父进程终止自身子进程并查残留、实际端口重绑；异常析构单独记录 |

异步与 GIL 的真实证据不足时，可在同一次启动中用有限的更长批次扩展观察窗口。
不要把注入 sleep 的 heartbeat 当成 SDMA 正在执行的证据。
“无计算侧拷贝”应限定被测提交区间；初始化、独立计算探针和校验读回另记。
支持 profiler 时保留设备/stream/SDMA 任务证据；仅有配置和源码时标成源码分派证据，不声称已测到硬件任务。

SDK 子提交错误应优先在独立 SDK 测试构建中用受控故障注入验证，不以破坏映射或越界设备访问制造故障。
没有该证据且所用代码仍丢失错误时，报告 `blocked-sdk-error-propagation`，交还规划者处理。
不扩大为自动重试、复制后生产读回、AIV 回退或换传输后端。

## 6 交付与报告

候选文件（尚不存在）：`ascend-mempool-test/scripts/verify_pool_sdma.py`、对应自运行 launcher 和 `K1_TICKET11_DEMO.md`。
复用 [独立启动工具](../../ascend-mempool-test/scripts/verify_bm_startup.py) 的结构，避免复制整套服务启动器。
由 high 根据实际 CLI 给出一条可执行命令；本文不提供尚不存在的伪命令。
日志写到 `/home/cryang/log/k1-ticket11-demo-<run-id>/`，失败返回非零。

报告至少包含：

- 实际 SGLang/demo commit、MF Python 模块和动态库路径/版本/构建标识、CANN/torch_npu 版本。
- host、PID/TID、NUMA、允许 CPU、实际线程 affinity、逻辑设备号及可获得的物理设备映射。
- 各清单的源/目标 owner、segment、GVA、字节、flags、调用数和校验摘要。
- submit/wait 返回码、时间点、控制线程推进次数、peer 读回结果、退出与端口证据。
- `functional_copy`、`completion`、`gil_progress`、`sdma_evidence`、`error_propagation` 分别给出 passed/failed/unverified。
- 4 参与者 smoke 与 16 参与者路径覆盖分别标记；未执行项如实标记。

同一测试最多 2 轮，尽量复用会话。CPU 仅保留清单 oracle 和任务状态/失败传播的关键测试。
实际 NPU 由用户运行；high 不因本文而获得远端访问授权。

## 7 交还规划者的门槛

提供代码引用、用户回传报告、最小失败复现及明确结论。
只有真实 copy、跨 NUMA/peer 可见性、执行器完成契约和 SDK 失败语义得到支持，才建议进入工程接入。
硬件后端证据的强度必须明示；缺证据时先核对，不自动当作通过。
下一阶段读取 [工程接入草案](ticket11-implementation-plan.md)，用实际结果替换其中假设，再请用户确认逐 op 路径。

## 本轮证据范围

2026-11-02 规划者仅阅读本地源码和文档，已重新核对 GitCode origin、HEAD、VERSION 与工作区。
SDK 具体行为以上述固定本地版本为依据；未 pull、未修改 SDK，实机二进制尚待核对。
