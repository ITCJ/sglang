---
title: SGLang 如何保留可复用前缀的 Index K
subtitle: 请求释放、缓存淘汰、跨层恢复是三件不同的事
template: doc
theme: blueprint
lang: zh
---

## A 先判断 P 到底释放了什么

下面讨论 DSA 模型的原生缓存路径。
代码基点是当前 checkout 的 d328af12bd。
已交叉查阅官方 HiCache 与 HiSparse 文档。

| A 已交接给 D 后，P 的状态 | B 怎样复用 |
|---|---|
| 请求已释放，L1 仍保留前缀 | 引用本地 HBM 中的 KV 与 Index K |
| HBM 页已淘汰，完整 L2 备份还在 | 从本地 DRAM 恢复 KV 与 Index K |
| 本地已无完整缓存，共享 L3 中存在 | 从存储获取完整状态，再恢复到 HBM |
| 所有可用层都缺少必要状态 | 缩短命中长度，重新计算缺失部分 |

请求退出不意味着 radix cache 马上丢弃 HBM 页。
真正淘汰后，原 HBM 槽位可以分配给其他请求。
残留字节不能再当作有效缓存。

## B L1 的 Index K 与 KV 使用关联索引

```tree
同一个可复用前缀
  radix tree | 保存前缀与 KV 位置、引用状态
  GPU 主 KV buffer | 保存 compact KV
  GPU IndexKeyCache | 保存 Index K 与必要 scale
```

DSATokenToKVPool 管理主 KV 与 IndexKeyCache。
两个 buffer 的物理地址可以不同。
对应页的身份和生命周期保持关联。
锁引用保护缓存页；解除引用后才有机会被淘汰。

P/D 交接也传输 DSA 状态，D 得到本地所需数据。
该交接不会把 D 的 HBM 自动变成 P 的 prefix cache。

## C HiCache 把 Index K 保存成附属数据

```sequence
P_HBM -> P_DRAM: backup 主 KV 与 INDEXER
P_DRAM --> 缓存控制器: 完成后确认可恢复
缓存控制器 -> P_HBM: 允许回收符合条件的页
新请求B -> 缓存控制器: 命中本地 L2 前缀
P_DRAM -> P_HBM: 恢复本层 KV 与 INDEXER
P_HBM --> B计算: 对应层所需数据已就绪
```

图中采用有常驻 L2 缓存的 HiCache 模式。
write-through 或 write-back 决定备份触发时机。
能否恢复，取决于备份是否真正完成。

DSAIndexerPoolHost 在 DRAM 中保存 Index K 与 scale。
INDEXER 作为 sidecar，跟随主 KV 的页索引传输。
按层恢复先提交本层各 buffer，再记录本层完成事件。
不同 buffer 不必放进一块连续 allocation。

## D 跨实例复用通过共享存储路径

```flow LR
生产实例完整状态 -> 共享L3: 配置备份后写入
共享L3 -> 消费实例L2: prefetch
消费实例L2 -> 消费实例HBM: 恢复主KV与INDEXER
消费实例HBM -> Prefill: 所需状态完整后计算
```

普通 HiCache 的 L1/L2 归各实例所有。
跨实例共享依赖后端配置和兼容的缓存身份。
Mooncake 中 KV 与 INDEXER 可保存成不同对象。
查找与读入都要考虑必需 sidecar 的完成范围。

示例：主 KV 有 10 页，INDEXER 只有连续的前 7 页。
这次 DSA 恢复最多使用完整的前 7 页。
无法仅凭主 KV 的 10 页宣布全部命中。

## E 对当前 mempool 设计的启示

可复用内容应包含 KV、Index K 和必要 scale。
缓存目录还需表明这些状态是否可恢复。
用户已接受将 prefill Index K 与必要 scale 存入 alive DRAM。
这份副本服务复用与 backup，具体布局和副本数待细化。

HiSparse 是另一条需要区分的路径。
它让 decode 从 host KV 中按需取回 sparse 数据。
当前校验要求 HiSparse 实例关闭 radix cache。
不能直接把其请求存储视为已经可供其他请求复用。

P 仍可用自己的普通 prefix cache 节省 prefill 计算。
HiSparse D 仍为新请求接收完整 prompt 主 KV。
P 的计算命中与 D 的 sparse 热数据命中是不同概念。

## F 源码与文档

- [完整源码核对记录](native-index-k-lifecycle.md)
- [GPU IndexKeyCache](../../python/sglang/srt/mem_cache/index_key_cache.py)
- [HiCache DSA stack](../../python/sglang/srt/mem_cache/hybrid_cache/hybrid_pool_assembler.py)
- [DRAM Index K pool](../../python/sglang/srt/mem_cache/pool_host/dsa.py)
- [按层传输](../../python/sglang/srt/mem_cache/l2_transfer.py)
- [官方 HiCache 层级与共享范围](https://docs.sglang.io/docs/advanced_features/hicache_design)
- [官方 P/D 与缓存备份配置](https://docs.sglang.io/docs/advanced_features/hicache_best_practices)
- [官方 HiSparse 说明](https://docs.sglang.io/docs/advanced_features/hisparse_guide)

本轮只核对源码和文档，没有运行硬件测试。
