---
template: doc
theme: shadcn
mode: light
lang: zh
title: sharing_plan.py：从 NUMA 配置到共享池计划
subtitle: 先确定节点，再决定分组；用一个四 rank 示例贯穿代码
source: ticket02-1 · d328af12bd · 代码讲解
---

`sharing_plan.py` 计算每个 rank 应加入哪个池。
它还计算 owner、BM 编号、内存大小、probe 和端口。
本模块只生成计划；BM 分配和设备映射由后续启动代码执行。

## A 先看调用顺序

建议先读文件后半部分的 `MempoolNumaConfig`。
它接收输入，再通过 `plan()` 返回 `SharingPlan`。
最后读计划上的校验和报告方法。

```flow LR
环境变量与映射文件 -> from_environment
from_environment -> MempoolNumaConfig
MempoolNumaConfig -> plan
主机拓扑与KV布局 -> plan
plan -> SharingPlan
SharingPlan -> validate_peer
对侧计划 -> validate_peer
SharingPlan -> report
```

| 入口 | 代码位置 | 负责什么 |
|---|---|---|
| `from_environment()` | 246行 | 读取环境与 JSON 文件，检查输入格式 |
| `plan()` | 289行 | 结合拓扑解析分配，生成共享组 |
| `validate_peer()` | 113行 | 对比已经取得的 P/D 计划 |
| `report()` | 181行 | 计算并输出每个 rank 的计划信息 |

`validate_peer()` 本身不联网。
调用者需要先拿到两侧计划，再显式调用它。

## B 五个数据结构各管一层

| 类型 | 主要字段 | 含义 |
|---|---|---|
| `MempoolNumaConfig` | share、旧列表、显式 map | 用户的配置输入 |
| `HostTopology` | host、ranks、online_nodes | 一台物理主机的已知拓扑 |
| `RankNuma` | host、rank、numa_node | 一个全局 TP rank 的节点分配 |
| `SharingGroup` | host、numa_node、members、nic_offset | 本侧共享一份 KV 的成员组 |
| `SharingPlan` | role、身份、layout、assignments、groups | 本侧整个 TP 实例的规划结果 |

这些 dataclass 使用 `frozen=True`。
正常调用不能重新赋值字段，成员集合也用 tuple 保存。
这样有助于保持启动后的分组稳定。
`frozen=True` 不自动验证数据；完整校验由入口方法完成。

一个 `SharingPlan` 只对应一个 role、instance 和 KV identity。
这些值约束整个计划，所以无需重复放进每个组的字典键。
规划器不会跨独立实例合并共享池。

`host` 由调用者提供，必须表示真实物理主机。
本模块不会自动探测远端主机，也不会识别主机别名。

## C 配置读取：先验格式，再验拓扑

`from_environment()` 只接受字面值 `0` 和 `1`。
没有共享开关时，默认取 `0`。
`true`、`01`、带空格的 `1` 都会报错。

显式 map 是一个 JSON 数组：

```json
[
  {"host": "host-a", "rank": 0, "node": 0},
  {"host": "host-a", "rank": 1, "node": 2}
]
```

| 检查阶段 | 检查内容 |
|---|---|
| 读取环境 | share 值合法；map 与旧节点列表互斥 |
| 读取 JSON | 非空数组；每条只有 host/rank/node |
| `_unique_fields()` | 同一 JSON 对象中不能重复出现 node 等字段 |
| `RankNuma.validate()` | host 非空；rank 是非负整数；node 在0到126内 |
| `plan()` | ranks 完整且唯一；host 匹配；节点在线 |

`type(rank) is int` 会拒绝布尔值。
Python 把 bool 视为 int 的子类，直接用 `isinstance` 会接受 `True`。

`parse_numa_nodes()` 位于20行。
它解析旧列表，并对照 `0-3,6` 形式的在线节点范围。
旧列表不能重复节点；显式 map 允许多个 ranks 使用同一节点。
因此不要用旧列表 `0,0,2,2` 表达逐 rank 映射。

| 输入情况 | 独立模式 | 共享模式 |
|---|---|---|
| 未指定节点 | `-1` 表示默认分配 | 报错 |
| 旧列表无效或拓扑未知 | 记录告警，回退默认分配 | 报错 |
| 显式 map 无效 | 报错 | 报错 |

`-1` 是独立模式内部生成的占位值。
它不能出现在用户提交的显式 map 中。

## D plan：先分配，再分组

`plan()` 先检查 TP 大小、role、身份、pool ID 和端口。
随后建立 `rank_hosts`，确保每个全局 rank 恰好归属一台主机。

如果配置了显式 map，就逐条核对 host 和在线节点。
否则按旧列表计算：

```python
node = nodes[rank % len(nodes)]
```

这里的 rank 是全局 TP rank。
到下一台主机后也不重新从列表头开始轮转。

四个 ranks 使用旧列表 `0,2`：

| TP rank | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| 分配的 NUMA node | 0 | 2 | 0 | 2 |
| share=0 的组 | `(0,)` | `(1,)` | `(2,)` | `(3,)` |
| share=1 的组 | `(0,2)` | `(1,3)` | `(0,2)` | `(1,3)` |

分组的关键代码位于393行：

```python
key = (
    assignment.host,
    assignment.numa_node,
    -1 if self.mempool_share else assignment.rank,
)
```

开启共享时，第三项统一为 `-1`，同机同节点的键相同。
关闭共享时，第三项使用 rank，每个 rank 的键都不同。
分配结果保持相同，只改变存储副本的规划方式。

分组前先按 rank 排序。
因此成员顺序固定，`members[0]` 就是 owner。
本例两个组的 owner 分别是 TP rank 0 和1。

## E 三种编号与内存所有权

不要把全局 TP rank 当作 BM participant。
BM participant 在每个共享组内部单独编号。

```python
member = 组内位置                 # 0 .. g-1
side = 0 if role == "prefill" else 1
participant = side * g + member  # 0 .. 2g-1
owner_participant = side * g
```

以成员 `(0,2)` 为例，g=2：

| 进程 | TP rank | member | BM participant | 计划贡献 |
|---|---|---|---|---|
| P0 | 0 | 0 | 0 | 完整 P pool |
| P2 | 2 | 1 | 1 | 0 |
| D0 | 0 | 0 | 2 | 完整 D pool |
| D2 | 2 | 1 | 3 | 0 |

每侧只规划一个物理 owner。
owner 表示内存贡献者；实际写入由后续 writer 分工决定。
其他成员将映射 owner 的存储；映射操作尚未接入。
共享不会把每个请求可用的 token 容量除以 g。

`group_layout()` 位于155行。
它保留原 prompt/decode 布局，只扩展 probe 空间：

```python
probe_bytes = max(base_probe_bytes, 64 * g)
probe_offset = payload_bytes_of_side + 64 * member
contribution = align_up(payload_bytes_of_side + probe_bytes, alignment)
rank_stride = max(P_contribution, D_contribution)
```

每名成员有独立64字节 probe 区间，避免互相覆盖。
这些区间位于 owner 的 payload 之后。
零贡献成员不会单独分配一块 probe 内存。
当前代码只预留区间；实际写入协议由02-2接入。

`PoolLayout` 原来的0/1参数在这里表示逻辑 P/D side。
因此 `layout.contribution_bytes(side)` 仍成立。
BM participant 需要在后续 manager 中转换到对应 owner。

## F 端口：兼容旧公式，隔开共享成员

每组使用 owner rank 确定 store 端口：

```python
store_port = base_port + owner_tp_rank
```

NIC 沿用已验证组件的分配方式。
每名成员预留 `2g` 个端口，一组预留 `2g²` 个端口。
`nic_offset` 累加前面各组的预留范围。

```python
nic_base = nic_port + group.nic_offset + 2 * g * member
actual_nic_port = nic_base + participant
```

本例用默认基址19000和25670：

| TP rank | 共享组 | store | P NIC | D NIC |
|---|---|---|---|---|
| 0 | `(0,2)` | 19000 | 25670 | 25672 |
| 1 | `(1,3)` | 19001 | 25678 | 25680 |
| 2 | `(0,2)` | 19000 | 25675 | 25677 |
| 3 | `(1,3)` | 19001 | 25683 | 25685 |

以上端口已用当前代码运行核对。
g=1时，每组预留两个端口，保留旧独立模式的公式。

`plan()` 检查端口上限和 store/NIC 范围重叠。
`validate_peer()` 再检查两侧实际的 `(host, port)` 冲突。
它不探测端口是否已被其他服务占用。

## G P/D 身份为什么不含 host 和 NUMA

`group_identity()` 位于164行。
它把逻辑配置序列化后计算 SHA-256。
排序 JSON 字段可使相同配置稳定得到相同摘要。

| 进入摘要 | 用途 |
|---|---|
| version | 区分身份格式 |
| instance_id、kv_identity | 区分实例和逻辑 KV 身份 |
| tp_size、mempool_share、members | 固定 TP 模式及成员关系 |
| pool_id、完整 group layout | 固定池配置及容量/probe/stride |
| writer_rule | 约定写入分工规则 |

P/D 可以部署在不同主机，也可以使用不同 NUMA 编号。
只要它们属于同一个逻辑组，就需要相同摘要。
所以 role、host 和 NUMA node 不进入这份摘要。
端口也不在摘要中，由单独的兼容校验检查。

`validate_peer()` 要求双方 role 相反。
它还比较身份、共享开关、layout、pool ID、store 基址和成员集合。
P 侧 `(0,2)/(1,3)` 不能搭配 D 侧 `(0,1)/(2,3)`。
不同物理主机可以使用相同数字端口。

`kv_identity` 当前来自调用者。
摘要没有读取模型权重，也没有比较真实 KV 内容。
`writer_rule` 当前只是合同标签；本文件没有执行分片写入。

## H report 与当前服务边界

`report()` 把组级信息展开为逐 rank 的字典。
它调用同一组 layout、身份和端口计算函数。
然后按全局 rank 排序，汇总本侧的计划贡献。

| 输出 | 含义 |
|---|---|
| group、owner_tp_rank | 共享对象及物理 owner |
| participant、owner_participant、world_size | BM 组内身份 |
| contribution_bytes、rank_stride_bytes | 计划贡献与最大 stride |
| probe_offset、store_port、nic_port | 计划偏移及端口 |
| pool_identity | 逻辑配置摘要 |
| allocation_status | 固定为 `planned` |
| placement_verified | 固定为 `false` |

`report()` 不会自动调用 `validate_peer()`。
报告成功只说明本侧计划已生成。
启动流程仍需交换对侧计划，并显式校验。

`require_rank_pair_startup()` 位于237行。
当前生产 config 和 manager 会调用这个阶段检查。
`share=1` 或显式 map 会报错并指向02-2。
旧分配器因此不会静默忽略新的配置意图。

规划入口与真实分配入口目前是分开的。
02-2将实现计划交换、BM 分配、映射和组生命周期。
本文件提供它们需要共同遵守的配置与编号合同。
