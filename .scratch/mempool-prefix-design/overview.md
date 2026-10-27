---
title: Mempool 前缀复用设计草案
subtitle: 先恢复计算所需的 HBM，再与 prefill 重叠填充 alive
template: doc
theme: blueprint
lang: zh
---

## A 两种存储用途

| 对象 | 保存什么 | 主要用途 |
|---|---|---|
| P 的 HBM | 命中 prefix 与新算 KV | 执行 prefill，含 chunk prefill |
| alive DRAM | 当前请求的 KV | 支持后续 decode 的 sparse 读取 |
| prefix mem pool | 跨请求保留的前缀 | 供下一次请求按 page 恢复 |

alive 与 prefix 不做物理去重。
一致性哈希只负责 prefix 放置。
alive 是一个逻辑 pool，内部可有多处区域和副本。
P/D 生成的 KV 默认各自本地保存。
入场时也可选择把 prompt 放到 D 附近。

## B 命中后的复制与计算

```sequence
Router -> 缓存进程: 选择源，预留目标，保护源页
缓存进程 -> P_HBM: 优先恢复命中 prefix
P_HBM --> P计算: 对应层的数据已就绪
P计算 -> P计算: prefill 未命中部分
缓存进程 -> P_alive: 后台复制命中段
P计算 -> P_alive: 只写新算段
P_alive --> D计算: 完整 prompt 已就绪
P计算 -> D计算: Index K 与 handoff
note D计算: 全部前置条件满足后开始 decode
```

命中段与新算段写入不同范围。
两条 DRAM 写入路径可与 prefill 重叠。
D 还要等待 Index K 和 handoff。
图中采用默认的 P 侧 prompt 存储。
若选 D 侧存储，两条 DRAM 写入都改向该目标。

## C 按层流水仍需学习与核实

```flow LR
恢复第1层 -> 计算第1层: 本层 ready
恢复第1层 -> 恢复第2层: 继续搬运
恢复第2层 -> 计算第2层: 本层 ready
计算第1层 -> 计算第2层: 模型依赖
```

此图仅表示候选依赖，不表示测得的执行时间。
每层计算前，必须拿到本层需要的历史 KV。
后续层的恢复可与当前层计算重叠。
L3 预取、HBM 按层恢复与 chunk prefill 要分别解释。

## D Router 统一目录

```tree
可共享 KV 目录
  prefix mem pool | 已保留的可恢复页
  alive prefill | 已完成且允许复用的页
请求私有状态
  alive decode | 活跃生成的新 KV
```

统一目录不要求统一物理地址。
hash 表示内容身份，ready 表示数据可用。
目录还需要布局、位置和 generation。
Index K 是附属模型数据，不是目录索引。

已确认：prefill Index K 与必要 scale 也存 alive DRAM。
这份附属副本供后续复用和 backup。
router 选择位置并预留容量。
相关 owner 协调分配具体页，router 不维护空闲页表。

## E B 复制复用 A

B 拿到自己的 HBM 与 alive 副本。
A 的源页保护到已有复制完成。
B 的后续 decode 不再依赖 A 的存活期。

```flow LR
A源页 -> 复制中: 临时保护
复制中 -> B副本: 完成并发布
复制中 -> A可回收: 等所有消费者完成
```

请求结束后，选择是否复制到 prefix。
发布 prefix 必须等待复制完成。
alive 回收必须等待计算与复制 drain。

## F 请求关联与副本索引

| attention 配置 | 一个 DP 分组 | 该组 KV 副本 |
|---|---|---|
| TP2 / DP4 | 两个 rank | 一个 NUMA 一份 |
| TP4 / DP2 | 四个 rank | 两个 NUMA 两份 |

两例的总 TP 都是 8，CP 为 1。
同内容副本使用相同页索引和 layout，base 可以不同。
请求下发后形成 KV group，关联实际 P/D 与位置。
设备可访问的候选位置可以大于本次关联范围。

P/D attention TP 不同时，demo 沿用 P 侧 prompt 副本。
这可能让超过两个 D reader 集中访问同一 NUMA。
按 D 拓扑补副本属于后续优化。

容量建议：必须两处复制时，不能直接相加空闲页数。
示意两处有 100 / 60 空闲页，最多承接 60 逻辑页。
若页号也一致，还需确认这些页在两处都可用。

## G 分散 alive 与直接 backup

示意每页 16 个 token，prompt 长 30。
alive 中 P 的末尾 14 个 token 与 D 数据仍各自保存。

```flow LR
P尾部14个 -> Prefix目标页: 写入 offset 0
D起始2个 -> Prefix目标页: 写入 offset 14
Prefix目标页 -> 发布可用: 全部数据与附属状态完成
```

异步复制直接形成目标页，无需先整理 alive。
源与目标元素布局必须适合直接复制。
页不连续或布局不同会增加复制段数。
backup 的提交开销与吞吐仍需实测。
D 段对齐方式与 prefix hash 放置单位尚未确定。

## H 阅读入口

- [完整草案与代码证据](design-draft.md)
- [SGLang 如何保留 Index K](native-index-k-explainer.html)
- [第一步：GPU 主线框架讲解 prompt](sglang-main-agent-prompt.md)
- [框架补充：HiSparse HTML 完善 prompt](sglang-main-hisparse-followup-prompt.md)
- [第二步：完整设计分析追加 prompt](sglang-main-design-followup-prompt.md)
- [历史 NUMA 组件结果](../ascend-mempool-numa-sharing/visuals/cd-v1-resume2/report.html)

本轮没有执行硬件测试。
本页的流程是设计方向，不代表已完成集成。
