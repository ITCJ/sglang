---
template: doc
theme: shadcn
mode: light
lang: zh
title: 两个部署例子：sharing_plan 如何分组
subtitle: 双机16卡 TP16；四机32卡 TP32，并展开 TP8 × DP4 的边界
source: 2026-10-15 · d328af12bd · 使用当前规划器计算
---

你确认的部署是双机1P+1D、四机2P+2D。
第一个例子每个 NUMA 放两个同实例 ranks。
第二个例子先用每机 TP32，每个 NUMA 放四个同实例 ranks。
最后再说明 TP8×DP4 如何改变共享边界。

## A 先固定计数口径和支持范围

**物理机、TP 实例、共享组和 BM 组是四层概念。**
一台机器可以运行一个 TP 实例，也可以运行多个实例。
TP rank 在每个实例内部编号，不按整个集群的卡数编号。

| 名称 | 本文含义 |
|---|---|
| TP 实例 | 一侧共同处理一份请求状态的 TP workers |
| 本侧共享组 | 同实例、同主机、同 NUMA 的一组 ranks |
| 本侧物理存储份数 | 共享开启时，每组一份 owner allocation |
| P/D 配对的 BM 组 | 把 P 侧和 D 侧对应成员放入同一个 BM world |
| g | 本侧一个共享组的成员数 |
| BM world_size | 一个配对组的 P/D participants 总数，等于2g |

一个 g=2 的 BM 组有4个 participants。
它规划两份物理存储：P 一份、D 一份。
不能把这两份存储算作两个 BM world。

**两例使用这些明确的示意假设：**

- NUMA 编号统一写成0到7。
- 卡号、设备编号和本侧 TP rank 按表中的顺序对应。
- 16卡机器中，相邻两卡分配到一个 NUMA。
- 32卡机器中，相邻四卡分配到一个 NUMA。
- 所有成员使用兼容的 compact KV 布局。

这些是配置示例，不是对真实硬件亲和性的探测结果。
真实部署应按设备拓扑生成 map。
规划器也不会自动验证实际物理页落点。

| 场景 | 当前纯规划器 | 当前正式 mempool 服务 |
|---|---|---|
| TP16、DP1 | 可以计算 | 共享路径仍等待02-2 |
| TP32、DP1 | 可以计算 | 被现有 TP16 限制拒绝 |
| TP8、DP4 | 没有 DP 维度；只能分别演示各 TP8 实例 | 被 TP16、DP1 限制拒绝 |

本文所有数值是计划结果，不是硬件测量。
TP32 能算出计划，不代表 TP32 服务已经支持。

## B 例一：两台16卡，每侧一个 TP16

P0 使用16张卡，运行一个 prefill TP16 实例。
D0 使用16张卡，运行对应的 decode TP16 实例。
P0 和 D0 的 TP rank 都是0到15。
D0 的 rank 不应编号成16到31。

```flow LR
[P0: TP ranks 0至15] -> [8个 P/D BM 组]: 每组2个P成员
[8个 P/D BM 组] -> [D0: TP ranks 0至15]: 每组2个D成员
```

两侧各有一份 `SharingPlan`，role 不同。
它们使用相同的 `instance_id=case1-pair-0`。
示例物理 host 分别是 `case1-p0` 和 `case1-d0`。

显式分配公式是：

```python
numa_node = tp_rank // 2
```

| NUMA | P0 的成员 | D0 的成员 | 各侧 owner TP rank | 各 BM 组 world_size | store 端口 |
|---|---|---|---:|---:|---:|
| 0 | 0, 1 | 0, 1 | 0 | 4 | 19000 |
| 1 | 2, 3 | 2, 3 | 2 | 4 | 19002 |
| 2 | 4, 5 | 4, 5 | 4 | 4 | 19004 |
| 3 | 6, 7 | 6, 7 | 6 | 4 | 19006 |
| 4 | 8, 9 | 8, 9 | 8 | 4 | 19008 |
| 5 | 10, 11 | 10, 11 | 10 | 4 | 19010 |
| 6 | 12, 13 | 12, 13 | 12 | 4 | 19012 |
| 7 | 14, 15 | 14, 15 | 14 | 4 | 19014 |

例如 NUMA2 的组是 `(4,5)`。
rank4 是 owner，因为它是排序后的第一名。
rank5 贡献0，但后续仍会拥有自己的 BM handle 和设备映射。

| 计数对象 | share=0 | share=1 |
|---|---:|---:|
| P0 的物理存储份数 | 16 | 8 |
| D0 的物理存储份数 | 16 | 8 |
| 全部署物理存储份数 | 32 | 16 |
| P/D 配对的 BM 组数 | 16 | 8 |
| 所有组的 participants 总数 | 32 | 32 |

共享减少物理副本和 BM 组数量。
每张卡对应的 worker 仍然参与，不会因为零贡献而消失。

## C 例一：完整拆开 NUMA0 的四个 participants

本组 `members=(0,1)`，所以 g=2。
组内 member 是0或1，与物理主机编号无关。

```python
participant = side * g + member  # P side=0，D side=1
owner_participant = side * g
```

为便于算具体容量，两例统一使用一套示例参数：

| 参数 | 示例值 |
|---|---:|
| layers | 78 |
| slots | 16 |
| P 每 slot 的 token 容量 | 4096 |
| D 每 slot 的 token 容量 | 8192 |
| compact dim | 512 + 64 = 576 |
| dtype | BF16，每元素2字节 |
| allocation 对齐 | 1 GiB |

`slots=16` 是请求槽位数，与 TP16 中的16没有绑定关系。
这里的容量也不是服务默认值，而是为比较选定的输入。

```python
P_payload = 78 * 16 * 4096 * 576 * 2  # 5,888,802,816 B
D_payload = 78 * 16 * 8192 * 576 * 2  # 11,777,605,632 B
probe_bytes = 64 * 2                  # 128 B
P_contribution = align_up(P_payload + 128, 1 GiB)  # 6 GiB
D_contribution = align_up(D_payload + 128, 1 GiB)  # 11 GiB
rank_stride = max(6 GiB, 11 GiB)                  # 11 GiB
```

贡献包含所有 layers、所有 slots 和对应 token 容量。
它不是单个 token 或单个请求的字节数。
stride 是共同地址间距，不是每个 participant 的实际贡献。

| worker | TP rank | member | participant | owner participant | 本地贡献 | probe 相对偏移 |
|---|---:|---:|---:|---:|---:|---|
| P0/r0 | 0 | 0 | 0 | 0 | 6 GiB | P_payload + 0 |
| P0/r1 | 1 | 1 | 1 | 0 | 0 | P_payload + 64 |
| D0/r0 | 0 | 0 | 2 | 2 | 11 GiB | D_payload + 0 |
| D0/r1 | 1 | 1 | 3 | 2 | 0 | D_payload + 64 |

P 的两个 probe 位于 P owner 的 allocation 中。
D 的两个 probe 位于 D owner 的 allocation 中。
零贡献成员不会为 probe 另建一份完整 KV。
owner 是内存贡献者；后续每个成员都可直接访问映射。

本组 `nic_offset=0`。
每个 member 预留4个端口，MemFabric 再加 participant 偏移。

| worker | NIC base | 加上的 participant | 实际 NIC 端口 |
|---|---:|---:|---:|
| P0/r0 | 25670 | 0 | 25670 |
| P0/r1 | 25674 | 1 | 25675 |
| D0/r0 | 25670 | 2 | 25672 |
| D0/r1 | 25674 | 3 | 25677 |

本组预留范围是25670到25677。
下一组 offset 增加8，使用25678到25685。
同组的 store 端口相同，因为成员连接同一个 store。

| 内存位置 | share=0 | share=1 |
|---|---:|---:|
| P0 | 16 × 6 = 96 GiB | 8 × 6 = 48 GiB |
| D0 | 16 × 11 = 176 GiB | 8 × 11 = 88 GiB |
| 两机合计 | 272 GiB | 136 GiB |

这是 BM KV contribution 的计划值，节省50%。
它不包含模型权重、HBM cache 和其他服务内存。
每个 reader 仍需要自己的完整 selected KV。
读取流量不会因为物理副本减半就自动减半。

## D 例一：这些输入怎样经过代码

P0 的拓扑输入是一台主机、16个全局 TP ranks：

```python
hosts = (HostTopology("case1-p0", tuple(range(16)), "0-7"),)
```

P0 的显式 map 按 `rank // 2` 生成16条记录。
例如前两条如下；完整文件已随页面保存。

```json
[
  {"host": "case1-p0", "rank": 0, "node": 0},
  {"host": "case1-p0", "rank": 1, "node": 0}
]
```

这段截取不能单独充当 TP16 的完整 map。
缺少 rank2到15时，`plan()` 会明确拒绝。

```flow LR
环境与完整map -> from_environment
from_environment -> MempoolNumaConfig
MempoolNumaConfig -> plan
拓扑与layout -> plan
plan -> P计划
P计划 -> validate_peer
D计划 -> validate_peer
P计划 -> report
```

| 代码步骤 | 本例得到的结果 |
|---|---|
| `from_environment()` | share=True；16条显式分配 |
| `plan()` 检查拓扑 | ranks 0到15完整且唯一 |
| map 校验 | host 匹配；节点全部属于0到7 |
| 排序和分组 | `(0,1), (2,3), …, (14,15)` |
| owner | `0,2,4,6,8,10,12,14` |
| NIC offset | `0,8,16,24,32,40,48,56` |
| `validate_peer()` | 两侧成员、layout和逻辑身份一致 |
| `report()` | 按 rank 输出计划贡献、编号、probe和端口 |

**旧节点列表与这里的显式 map 有不同含义。**
如果使用旧列表 `0,1,2,3,4,5,6,7`，代码按 rank 取模。
它会得到下面的组：

| NUMA | 显式 `rank // 2` | 旧列表 `rank % 8` |
|---|---|---|
| 0 | `(0,1)` | `(0,8)` |
| 1 | `(2,3)` | `(1,9)` |
| 2 | `(4,5)` | `(2,10)` |
| 7 | `(14,15)` | `(7,15)` |

两种分配都有8组，组大小都是2，但成员关系不同。
P 用第一种、D 用第二种，会被 `validate_peer()` 拒绝。
不能只比较两侧的组数和 NUMA 数量。

D 可以使用不同 NUMA 编号，只要对应成员仍相同。
例如 P 的 `(0,1)` 在 node0，D 的 `(0,1)` 在 node6，逻辑上可以配对。
实际在线节点和所需硬件映射仍须单独满足。

## E 例二：四台32卡，先按每机 TP32 展开

你确认这里是两台 P、两台 D。
每台机器有32张卡，所以每机可容纳一个完整 TP32 实例。
为了使用全部128张卡，本文选择两对独立 P/D 实例。
实际服务路由仍是未来接入内容。

```flow LR
[P0: case2-p0, TP32] -> [D0: case2-d0, TP32]: case2-pair-0
[P1: case2-p1, TP32] -> [D1: case2-d1, TP32]: case2-pair-1
```

| 物理机 | role | TP rank 范围 | 配对的 instance_id | 占用卡数 |
|---|---|---|---|---:|
| P0 | prefill | 0到31 | case2-pair-0 | 32 |
| D0 | decode | 0到31 | case2-pair-0 | 32 |
| P1 | prefill | 0到31 | case2-pair-1 | 32 |
| D1 | decode | 0到31 | case2-pair-1 | 32 |

总共四份本侧计划、两对可匹配的 P/D 计划。
P0 的 rank0 和 P1 的 rank0 属于不同 TP 实例。
它们的 rank 数字相同，但 `instance_id` 不同。
单个 `SharingPlan` 不能把这两个 rank0 混成一个 rank。

每台主机使用显式分配：

```python
numa_node = tp_rank // 4
```

下面的分组表分别适用于两对 P/D：

| NUMA | 各侧成员 | owner TP rank | g | BM world_size | NIC offset | store 端口 |
|---|---|---:|---:|---:|---:|---:|
| 0 | 0,1,2,3 | 0 | 4 | 8 | 0 | 19000 |
| 1 | 4,5,6,7 | 4 | 4 | 8 | 32 | 19004 |
| 2 | 8,9,10,11 | 8 | 4 | 8 | 64 | 19008 |
| 3 | 12,13,14,15 | 12 | 4 | 8 | 96 | 19012 |
| 4 | 16,17,18,19 | 16 | 4 | 8 | 128 | 19016 |
| 5 | 20,21,22,23 | 20 | 4 | 8 | 160 | 19020 |
| 6 | 24,25,26,27 | 24 | 4 | 8 | 192 | 19024 |
| 7 | 28,29,30,31 | 28 | 4 | 8 | 224 | 19028 |

P0/node0 和 P1/node0 属于不同物理主机。
它们也属于不同实例，因此各有自己的 owner allocation。
相同的节点编号不表示同一块内存。

两对实例可以使用相同的数字端口，因为主机不同。
它们也可以各自使用数值 `pool_id=104`。
BM world 的连接端点和逻辑 instance 必须保持隔离。
仅靠相同或不同的数字 pool ID 不能代替实例隔离。

## F 例二：八个 participants 与全四机的贡献

看 P0/D0 的 NUMA0 组，`members=(0,1,2,3)`。
P 的 participant 为0到3，D 的 participant 为4到7。
两侧 owner participant 分别是0和4。

| worker | member | participant | 本地贡献 | 实际 NIC 端口 |
|---|---:|---:|---:|---:|
| P0/r0 | 0 | 0 | 6 GiB | 25670 |
| P0/r1 | 1 | 1 | 0 | 25679 |
| P0/r2 | 2 | 2 | 0 | 25688 |
| P0/r3 | 3 | 3 | 0 | 25697 |
| D0/r0 | 0 | 4 | 11 GiB | 25674 |
| D0/r1 | 1 | 5 | 0 | 25683 |
| D0/r2 | 2 | 6 | 0 | 25692 |
| D0/r3 | 3 | 7 | 0 | 25701 |

这里每个 member 的 NIC base 相差8。
一个组预留 `2 × 4² = 32` 个端口。
NUMA0 预留25670到25701，NUMA1从25702开始。
全部8组预留25670到25925。

probe 从128字节扩展为256字节。
成员0到3使用 payload 后的0、64、128、192偏移。
在本例的1 GiB对齐下，P/D贡献仍为6/11 GiB。
对齐前的 probe 变大，不一定使对齐后的 allocation 增大。

| 全部署计数 | share=0 | share=1 |
|---|---:|---:|
| P侧两机的存储份数 | 64 | 16 |
| D侧两机的存储份数 | 64 | 16 |
| 四机存储份数 | 128 | 32 |
| P/D 配对的 BM 组数 | 64 | 16 |
| 所有组的 participants 总数 | 128 | 128 |

共享模式的16个 BM 组，每组都有8个 participants。
因此参与进程总数是 `16 × 8 = 128`。

| 内存位置 | share=0 | share=1 |
|---|---:|---:|
| 每台 P | 32 × 6 = 192 GiB | 8 × 6 = 48 GiB |
| 每台 D | 32 × 11 = 352 GiB | 8 × 11 = 88 GiB |
| 两台 P 合计 | 384 GiB | 96 GiB |
| 两台 D 合计 | 704 GiB | 176 GiB |
| 四机合计 | 1088 GiB | 272 GiB |

在相同容量输入下，计划贡献减少75%。
这不等于吞吐提升4倍，也不等于读取流量降至四分之一。

**如果你要的是单个 TP32 横跨两台 P，计数会不同。**
一个 TP32 总共只需要32个 ranks。
平均分到两台32卡 P，每机只使用16张卡。
D侧相同，总共使用64张卡，另64张闲置。
如果每侧跨两机并用满64张卡，那是 TP64 的例子。

跨主机的 TP 实例可以用多个 `HostTopology` 描述。
但共享组仍按 host 切开，不会把两台机器的 node0 合并。

## G 回到 TP8×DP4：为什么不能直接当作 TP32

这里先按普通 DP 理解：四个独立 TP8 请求副本。
每台机器使用 `8 × 4 = 32` 张卡。
每个副本都有自己的 TP rank0到7和请求状态。
不同 DP 副本的 KV 内容通常不同，即使模型和 dtype 相同。

**方案一：每个副本占连续8张卡。**
继续假设每四张卡对应一个 NUMA。

| DP副本 | 本机卡号 | 占用NUMA | 本副本TP ranks | 本副本共享组 |
|---|---|---|---|---|
| DP0 | 0到7 | 0、1 | 0到7 | `(0,1,2,3)`、`(4,5,6,7)` |
| DP1 | 8到15 | 2、3 | 0到7 | `(0,1,2,3)`、`(4,5,6,7)` |
| DP2 | 16到23 | 4、5 | 0到7 | `(0,1,2,3)`、`(4,5,6,7)` |
| DP3 | 24到31 | 6、7 | 0到7 | `(0,1,2,3)`、`(4,5,6,7)` |

例如物理卡10属于 DP1，本副本内的 TP rank 是2。
它的节点是2，member 是2。
如果它在 P侧，对应 participant 就是2。
物理卡号10不应直接填成这个 TP8 计划的 rank10。

每个 DP 副本得到2组，整台机器共8份存储。
数字上与 TP32 的8份相同，数据含义却不同：

| 对比 | 每机一个 TP32 | 每机四个普通 TP8 副本 |
|---|---|---|
| 逻辑请求状态 | 1套 | 4套，各自独立 |
| 一套 KV 的本机副本数 | 8 | 2 |
| 每机物理存储份数 | 8 | 4 × 2 = 8 |
| 若每实例 slots=16 | 1套16个槽位 | 4套各16个槽位 |
| rank 编号 | 一套0到31 | 四套0到7 |

这不构成模型显存、吞吐或请求容量的等价比较。
这里只演示 BM 共享边界如何变化。

**方案二：每个 NUMA 放四个不同 DP 的 ranks。**
假设 node n 放 DP0/rn、DP1/rn、DP2/rn、DP3/rn。
每个 DP 的八张卡就分散在八个 NUMA 上。

```tree list
同一物理主机的 NUMA0
  DP0 的 TP rank0 | 请求集合A，只能共享A的KV
  DP1 的 TP rank0 | 请求集合B，只能共享B的KV
  DP2 的 TP rank0 | 请求集合C，只能共享C的KV
  DP3 的 TP rank0 | 请求集合D，只能共享D的KV
```

这四个 ranks 不能因为同机同节点就合并。
每个 DP 在每个节点只有一个成员，所以 g=1。
每台机器共有 `4 × 8 = 32` 份存储，不能在这里去重。

**现有规划器怎样表达，哪些还没有实现？**
它没有 `dp_rank` 字段，也没有 DP 请求路由。
未来可以让每个 DP 副本单独生成一对 TP8 计划。
对应 P/D 副本共享一个 instance_id，不同副本使用不同身份。

| 副本 | 示例 instance_id | store基址 | NIC基址 |
|---|---|---:|---:|
| DP0 | future-dp-pair-0-replica-0 | 19000 | 25670 |
| DP1 | future-dp-pair-0-replica-1 | 19100 | 25770 |
| DP2 | future-dp-pair-0-replica-2 | 19200 | 25870 |
| DP3 | future-dp-pair-0-replica-3 | 19300 | 25970 |

这些基址为示例，各副本预留的64个 NIC 端口互不重叠。
只改变 instance_id 不会改变监听端口，仍可能冲突。
当前 `validate_peer()` 只检查一对计划，不管理跨实例端口资源。
未来还需要副本配对、slot/session、生命周期和发布依赖的隔离。
把 DP ranks 扁平化成一个 TP32 计划会丢失这些边界。

**SGLang 的 DP attention 另有参数语义。**
当前代码在启用它时计算 `attn_tp_size = tp_size / dp_size / cp_size`。
所以 TP8、DP4、CP1 对应 attention TP2，不能按普通 DP 的32卡解释。
若想表达全局32卡中的四个 attention TP8 组，算式是32/4=8。
这只是当前代码的编号关系，不表示 mempool 已支持该模式。

## H 可复现输入、检查结果和源码落点

两例已通过当前 `MempoolNumaConfig.plan()` 实际计算。
每对 P/D 都调用了 `validate_peer()`。
附带文件保存全部 ranks 的映射、端口和贡献，无需手工补齐表格。
DP附例只计算四份互相隔离的 TP8 计划。
它没有启动 DP 服务，不能作为 DP 支持的证据。

| 文件 | 用途 |
|---|---|
| [calculate.py](examples/sharing-plan-deployments/calculate.py) | 重算全部示例，校验组数、成员、身份和总贡献 |
| [summary.json](examples/sharing-plan-deployments/summary.json) | 两例汇总与容量输入 |
| [双机P拓扑](examples/sharing-plan-deployments/case1-p0-shared-topology.json) | 完整16-rank主机拓扑 |
| [双机P映射](examples/sharing-plan-deployments/case1-p0-shared-map.json) | 完整16条显式分配 |
| [双机P报告](examples/sharing-plan-deployments/case1-p0-shared-report.json) | P侧16个 ranks 的逐项计划 |
| [双机D报告](examples/sharing-plan-deployments/case1-d0-shared-report.json) | D侧编号、probe、贡献与端口 |
| [四机P0映射](examples/sharing-plan-deployments/case2-p0-shared-map.json) | 完整32条显式分配 |
| [四机P0报告](examples/sharing-plan-deployments/case2-p0-shared-report.json) | 第一对实例的P计划 |
| [四机D0报告](examples/sharing-plan-deployments/case2-d0-shared-report.json) | 第一对实例的D计划 |
| [四机P1报告](examples/sharing-plan-deployments/case2-p1-shared-report.json) | 第二对实例的P计划 |
| [四机D1报告](examples/sharing-plan-deployments/case2-d1-shared-report.json) | 第二对实例的D计划 |

脚本仅依赖 msgspec，不导入 NPU runtime。
在安装了 msgspec 的 Python 环境中，从仓库根目录运行：

```bash
python3 .scratch/ascend-mempool-numa-sharing/examples/sharing-plan-deployments/calculate.py
```

报告里 `allocation_status` 都是 `planned`。
`placement_verified` 都是 `false`。
本次没有执行 BM 分配、远端握手或 NPU 测试。

| 关注的问题 | 当前代码落点 |
|---|---|
| 输入读取与冲突 | `sharing_plan.py:246`，`from_environment()` |
| 单TP实例的rank完整性 | `sharing_plan.py:318`，`rank_hosts` |
| 同机同NUMA合组 | `sharing_plan.py:390`，排序及分组键 |
| owner和participant | `sharing_plan.py:181`，`report()` |
| P/D对应和端口冲突 | `sharing_plan.py:113`，`validate_peer()` |
| probe及贡献 | `sharing_plan.py:155`，`group_layout()` |
| TP16、DP1服务限制 | `config.py:44`，`required` |
| 普通DP启动各TP实例 | `data_parallel_controller.py:371` |
| DP attention的TP划分 | `data_parallel_controller.py:682` |

源码分别位于生产 NPU mempool 目录和 SGLang managers 目录。
本页只增加解释材料，没有放宽 TP/DP 限制或修改生产代码。
