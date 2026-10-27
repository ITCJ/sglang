---
title: SharingPlan 代码导读
subtitle: d328af12bd 基础 · 2026-10-22 更新为连续 NUMA 分配
template: doc
theme: blueprint
lang: zh
source: python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py
---

这份代码计算池的部署计划。它输出分组、容量贡献和连接参数。实际分配、映射和读写仍由后续 manager 接入。

## A 入口与数据结构

```flow
环境变量及映射文件 -> MempoolNumaConfig: from_environment
MempoolNumaConfig -> SharingPlan: plan 加入拓扑与布局
SharingPlan -> P/D兼容检查: validate_peer
SharingPlan -> 逐rank报告: report
```

`validate_peer()` 需要调用者另行调用。`plan()` 和 `report()` 都不会自动交换两端计划。

| 结构 | 保存什么 |
|---|---|
| HostTopology | 一台物理主机、所含 TP ranks、在线 NUMA |
| RankNuma | 一个 rank 希望使用的主机和 NUMA |
| SharingGroup | 一份副本的成员、NUMA、端口偏移 |
| SharingPlan | 一侧的完整计划、布局、身份、告警 |
| MempoolNumaConfig | 共享开关、旧列表、显式映射 |

这些 dataclass 使用 `frozen=True`，生成后不能直接重新赋值。校验由配置入口和 `plan()` 完成，冻结本身不负责校验。

## B 解析配置，再验证它能否用于这台机器

`from_environment()` 读取共享开关，并解析可选的 JSON 映射。

| 输入 | 规则 |
|---|---|
| 共享开关 | 只接受字符串 `0` 或 `1`；默认 `0` |
| 旧 NUMA 列表 | 保留原字符串，稍后结合主机拓扑检查 |
| 显式 JSON 映射 | 非空数组；每项恰好包含 host、rank、node |
| 两种 NUMA 配置 | 不能同时提供；旧变量为空字符串也算提供 |

`_unique_fields()` 拒绝 JSON 中重复的字段名。`RankNuma.validate()` 拒绝空 host、负 rank 和非法节点。代码用 `type(x) is int`，也会拒绝布尔值。

`parse_numa_nodes()` 接受旧列表，如 `0,2`。在线节点字符串另可写成 `0-3,6`。函数保留旧列表顺序，拒绝重复、负数、离线节点及 127 以上的节点。

节点列表的顺序决定连续分配的顺序。规划器消费调用者给出的拓扑；它不自动发现远端 NUMA。

## C plan：先分配 NUMA，再合并分组

贯穿例子：P、D 各 TP4；每侧一台机器；旧 NUMA 列表为 `0,2`。

```python
node = contiguous_numa_node(nodes, local_rank, len(host.ranks))
```

| TP rank | NUMA | share=0 的组 | share=1 的组 |
|---|---|---|---|
| 0 | 0 | (0,) | (0, 1) |
| 1 | 0 | (1,) | (0, 1) |
| 2 | 2 | (2,) | (2, 3) |
| 3 | 2 | (3,) | (2, 3) |

2026-10-22 起，默认改为连续均分。每台主机先将自己的 ranks 排序，再用本机序号分配。

不能整除时，列表靠前的节点多分一个 rank。节点多于 ranks 时只使用前若干节点。显式映射仍可指定非相邻的 `(0,2)`、`(1,3)`。

`contiguous_numa_node()` 用 `divmod(rank_count, len(nodes))` 算每节点的基本人数和余数。前面的余数个节点多分一个，其余节点按基本人数分配。TP16 配 8 个节点时每节点 2 个 rank，配 4 个节点时每节点 4 个。

`plan()` 检查拓扑中每个 rank 恰好出现一次。显式映射还要覆盖全部 ranks，且 host 与拓扑一致。

| NUMA 输入情况 | 独立模式 | 共享模式 |
|---|---|---|
| 节点列表有效 | 按列表连续均分 | 按列表连续均分 |
| 旧列表无效 | 记告警，使用默认分配 | 报错 |
| 未给列表或映射 | 使用默认分配 | 报错 |
| 显式映射无效 | 报错 | 报错 |

默认分配用内部标记 `numa_node=-1`。它表示未指定节点，不能作为显式节点输入。

确定节点后的核心分组代码：

```python
assignments.sort(key=lambda assignment: assignment.rank)
key = (
    assignment.host,
    assignment.numa_node,
    -1 if self.mempool_share else assignment.rank,
)
members.setdefault(key, []).append(assignment.rank)
```

共享模式的第三项相同，因此同机同 NUMA 的 ranks 合组。独立模式把 rank 放进 key，因此不会合组。

排序让成员顺序稳定。`members[0]` 是 owner，也就是组内最小的 TP rank。组大小不固定，四成员组在语法上合法，性能仍需另测。

## D 三种编号与 owner

观察组 `(0,1)`。组大小 `g=2`，P、D 合计有 `2g=4` 个通信参与者。

```python
side = 0 if role == "prefill" else 1
participant = side * g + member
owner_participant = side * g
```

| 进程 | TP rank | 组内 member | participant | 是否贡献存储 |
|---|---|---|---|---|
| P0 | 0 | 0 | 0 | P 侧完整容量 |
| P1 | 1 | 1 | 1 | 0 |
| D0 | 0 | 0 | 2 | D 侧完整容量 |
| D1 | 1 | 1 | 3 | 0 |

`owner_tp_rank` 在两侧都是 0。`owner_participant` 在 P 是 0，在 D 是 2。这里的 `world_size=4` 是单个池通信组的大小。

owner 负责贡献物理存储。其他参与者仍需要访问映射，贡献为零不代表没有访问权。owner 也不等于唯一 writer，更不是请求生命周期中的 owner hold。

P、D 各有自己的存储区域。共享组减少同侧的重复副本；它不把 prompt KV 和 decode KV 合成同一份数据。

## E group_layout：KV 容量、probe 与 stride

`group_layout()` 保留原来的 prompt/decode 布局，只扩展 probe 区域：

```python
probe_bytes = max(original_probe_bytes, 64 * g)
```

每个成员有独立的 64 字节探测区域，位于该侧 KV payload 之后。它用于后续映射和会话检查，本提交不写入 probe。

```text
某侧 owner 的区域
[ 各层 KV payload ][ probe 0 ][ probe 1 ][ 对齐填充 ]
```

布局计算来自 `layout.py`：

```python
payload = layers * slots * tokens * heads * dim * 2  # BF16
probe_offset = payload + 64 * member
contribution = align_up(payload + probe_bytes, alignment_bytes)
rank_stride = max(P_contribution, D_contribution)
```

只有 owner 记入完整 contribution，其余成员为 0。共享不会把每份副本的 token 容量除以组大小。

P/D 的 tokens 可以不同；层数、槽数、head、dim 和 dtype 必须一致。stride 使用两侧较大贡献值，不等于两侧都实际贡献这么多内存。默认对齐是 1 GiB，所以小 payload 的计划贡献也可能达到 1 GiB。

`PoolLayout` 的参数 0/1 在这里代表 P/D side。它不能直接用通信 participant 替换。例如本例 D owner participant 是 2，但计算 D 布局仍传 side=1。

## F 端口为什么这样计算

`store_port = base_port + owner_tp_rank`。同组成员使用同一 store 端口。它用于启动会合，和 KV payload 不同。

代码按以下方式预留通信端口：

```python
nic_base = nic_port + group.nic_offset + 2 * g * member
actual_port = nic_base + participant
next_group_offset = group.nic_offset + 2 * g * g
```

当前 SDK 约定会在传入的基址上加 participant。代码为每个 member 留出 2g 个端口，因此一组预留 2g² 个端口。这些是预留范围，不是每个端口都开启监听。

例子使用默认 `base_port=19000`、`nic_port=25670`。两组的偏移分别为 0、8。

| TP rank | 共享组 | Store 端口 | P 实际端口 | D 实际端口 |
|---|---|---|---|---|
| 0 | (0,1) | 19000 | 25670 | 25672 |
| 1 | (0,1) | 19000 | 25675 | 25677 |
| 2 | (2,3) | 19002 | 25678 | 25680 |
| 3 | (2,3) | 19002 | 25683 | 25685 |

`plan()` 拒绝端口溢出和 Store/NIC 预留范围重叠。`validate_peer()` 另检查同一物理主机上的跨侧 NIC 冲突。它不探测端口是否被其他进程占用。

## G 身份、两端校验与报告

`group_identity()` 对配置字典做规范 JSON 编码，再计算 SHA-256。它包含实例、KV 身份、TP 大小、共享开关、成员、pool ID、布局和 writer 规则。

这是池配置的指纹。它不读取 KV 内容，也不是 prefix 页哈希。host、NUMA、role 和端口不进入指纹，因此对应 P/D 组可以得到相同身份。

`writer_rule="contiguous-rows-v1"` 记录约定版本。真正的行分配和写入尚未实现。

| validate_peer 检查 | 要求 |
|---|---|
| role | 一侧 prefill，一侧 decode |
| instance、KV identity、共享开关、pool ID、Store 基址 | 相同 |
| 完整布局 | 相同，包括约定的两侧容量 |
| 各组的成员序列 | 相同 |
| host、NUMA、NIC 基址 | 可以不同，但实际端点不能冲突 |

因此，这个版本的计划仍要求 P/D 相同的 TP 分组。它不能直接表示新版完整方案中的异构 P/D 分组。

`report()` 按 TP rank 排序输出成员明细。顶层总贡献只汇总本侧 owner 的计划容量。`allocation_status="planned"` 和 `placement_verified=false` 明确标记其证据范围。

## H 当前服务如何接入，以及哪些事情仍待完成

```flow
正式服务配置 -> 配置检查: from_environment
配置检查 -> 旧rank配对启动: share=0 且没有显式map
配置检查 -> 提前报错: share=1 或提供显式map
CPU预览 -> 分组计划: from_environment 后调用 plan
分组计划 -> 报告文件: report
```

`require_rank_pair_startup()` 是阶段限制。旧 allocator 尚不能消费共享组计划，因此先拒绝新配置。

| 事项 | d328af12bd 的范围 |
|---|---|
| 配置、分组、容量和连接参数 | 已实现纯 CPU 规划 |
| P/D 计划兼容检查 | 函数已实现；实际交换待接入 |
| 共享池创建、join、设备映射 | 待后续实现 |
| 多 rank 写入分工、读写就绪、drain | 待后续实现 |
| Main Router、prefix 目录、page allocator | 不属于这个文件 |
| 物理 NUMA 落点、真实吞吐和延迟 | 需要硬件证据 |

历史 d328af12bd 交付记录包含 368 项 CPU 回归通过，以及 34 项专项检查通过。连续分配修改的检查记录见同目录 ticket02 的最新 Comments；设备验证由用户执行。

新 demo 可以复用配置校验、分组和编号表达。dense 的单份共享 MLA 与分片 KDA 状态，需要独立的布局和参与者接线。
