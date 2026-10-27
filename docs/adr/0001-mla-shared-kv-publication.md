# MLA 共享 KV 复用既有 TP 通信完成发布

2026-10-12 用户确认：MLA NUMA 共享 mempool 的首版将本步新 KV 直接从各 TP
rank 的本地计算结果供给 attention，历史 KV 通过本地 sparse cache 或共享
mempool 读取。各 rank 分工写入共享 KV，并复用 attention 后已有的 TP
collective，作为后续步骤读取这些 KV 的完成边界。

当前 mempool 写入和同层 KV 读取之间没有 TP collective，已有通信发生在
attention 之后。本地供给新 KV 消除了同层读取 peer 未完成写入的依赖，从而
避免为共享存储新增逐层跨 rank barrier。首版写入不跨 decode step 拖延，
无需引入通用多步 pending 队列；分工粒度由性能测试选择。

该决定要求既有 collective 的前置依赖覆盖共享组全部 writer 的 copy 完成，
并验证其他 readers 对 BM 的可见性和 Graph capture/replay 行为。独立 write
stream 需在原有 collective 前接入本地完成事件；本步本地供给与历史读取
不因本步 BM 写入增加等待。
已有 TP 通信的 CPU 调用返回不等于设备工作全部完成。

同日用户要求第二阶段细化并行设计、避免影响性能。因此，早期“首版保持
producer stream”的限制调整为保留它作对照，同时评估 compact KV ready 后
提前提交的独立 write stream。写入可与 indexer、历史获取、attention/o_proj
的无依赖部分重叠；默认调度由实测决定，不能以增加 stream 代替性能证据。
P 可并行推进 native handoff 与 BM 写入，但所有 prompt 分片完成后才能发布
KV_READY；source 和 metadata 必须有界并保留到最后消费者完成。

用户随后明确两个并行关系：current KV 填入 selected buffer 与历史 UniDexCopy
独立提交，只在 attention/refill 消费所需数据时汇合；write(L) 可以与同一
decode step 的 attention(L+1) 并行。因各层 compact KV 独立，下一层不消费
L 的 BM 新 KV，完成边界可后移到 TP(L+1) 前，最后一层在尾部已有 TP 前收齐。
同层 join 保留为对照；不同写回候选须在下一次同层历史读取前完成发布。

一层滞后使用有界 source/metadata staging，单执行 stream 下以两组交替为
最小候选。等待上一层的具体完成事件，不等整个 writer stream；下一次复用
前同时满足 local current-copy 与 writer 完成。尾部等待捕获在模型 Graph
内部，先于最后已有 TP 和 forward completion snapshot，不依赖 replay 时
不会执行的 Python 层回调。普通 scheduler overlap 保持有效。

范围、代码证据和后续决策见[设计讨论](../../.scratch/ascend-mempool-numa-sharing/design.md)。
具体 DAG 和对照矩阵见[执行设计](../../.scratch/ascend-mempool-numa-sharing/execution-design.md)。
