# 原生 SGLang 中 P/D 交接后 Index K 的生命周期

日期：2026-10-17。核对对象：当前 checkout `d328af12bd` 中的 GPU `DSATokenToKVPool`、原生 P/D 和 UnifiedRadixCache 路径。该分支含定制代码；以下明确采用未开启定制 mempool 的原生分支，不声称等于今日上游所有配置。

## 核心结论

**P 结束请求，不等于 P 的 prefix KV 已从 HBM 驱逐。** 开启正常 prefix cache 时，原生 P/D 交接完成会结束 P 的请求占用，把可缓存的页继续交给 P 的本地 radix cache 管理。DSA 的 compact KV 与对应 Index K/scale 都还在 P 的设备池里；后续 B 可以复用这些页。D 同时拥有自己收到的一份数据。

只有缓存真正被驱逐、或配置不保留缓存时，才进入“P 已经没有这份设备数据”的情况。此时能否复用，取决于是否存在配置允许访问、并已完成备份的其他缓存层。没有有效缓存副本就是 miss，对未命中的部分重新 prefill；不能仅因为 A 仍在 D 上执行，就推定 P 可以自动从 D 的活跃请求 HBM 恢复前缀。

## 1. 请求释放和缓存驱逐是两件事

原生 P/D 完成链如下：

1. P 轮询发送结果，在 `KVPoll.Success` 后标记其 P 请求完成；未启用定制 mempool 的分支调用 `release_kv_cache(req, self.tree_cache)`。源码：[prefill.py:1027](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:1027)。
2. `release_kv_cache` 先调用 `tree_cache.cache_finished_req`，最后才释放 request-to-token pool 的请求槽并标记该请求已释放。源码：[common.py:287](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/common.py:287)、[common.py:313](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/common.py:313)。
3. UnifiedRadixCache 在 `is_insert=True` 时取得请求实际拥有的 token 与 KV indices，按可缓存边界截断，然后将 page-aligned key 和 **indices 的副本**插入树。复制的是映射索引，不是另复制一整份 KV tensor。源码：[unified_radix_cache.py:981](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:981)、[unified_radix_cache.py:1020](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:1020)。
4. 同一函数释放不可缓存的尾部，并解除请求对树节点的锁；已被树接收的有效页继续属于 cache。禁用 cache 时则直接释放该请求的 KV 行。源码：[unified_radix_cache.py:1071](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:1071)、[unified_radix_cache.py:1093](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:1093)、[unified_radix_cache.py:975](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:975)。

实际驱逐有独立路径：tree core 返回 device frees，FullComponent 把相应页交还 allocator；之后其他请求可覆盖这些页。源码：[unified_radix_cache.py:759](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:759)、[components/full.py:504](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_cache/components/full.py:504)、[allocator/paged.py:270](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/allocator/paged.py:270)。页释放是可再分配，不必理解成立即把 GPU tensor 字节清零。

## 2. 为什么本地 prefix 命中会同时保留 Index K

`DSATokenToKVPool` 继承 MLA pool，在 compact KV 之外创建 `IndexKeyCache`。每层 index buffer 按 page 存储，行宽包含 Index K 数据以及量化 scale；不写 Index K 的层使用 0 行占位。源码：[memory_pool.py:4871](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/memory_pool.py:4871)、[memory_pool.py:4951](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/memory_pool.py:4951)、[index_key_cache.py:14](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/index_key_cache.py:14)。

它不是 radix tree 里另一组任意生命周期的 bytes：这些 index pages 通过对应的 KV page indices 寻址。例如 CPU 保存和恢复都用 `indices[::page_size] // page_size` 找到 index pages。源码：[index_key_cache.py:116](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/index_key_cache.py:116)、[index_key_cache.py:136](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/index_key_cache.py:136)。

新请求匹配树，得到 `match_result.device_indices` 作为 `prefix_indices`。因此，同实例命中原有设备页时，使用该页身份就能找到既有 compact KV 和 Index K/scale；无须因为原请求 A 结束，就单独从 D 找回一份 Index K。源码：[schedule_batch.py:1617](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/managers/schedule_batch.py:1617)、[schedule_batch.py:1633](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/managers/schedule_batch.py:1633)。

## 3. P/D 传输确实包含 Index K，但这不是自动回读 D

DSA pool 的 `get_state_buf_infos` 暴露 index buffer 指针、大小和 page item size；state 注册把它列入 `StateType.DSA`。源码：[memory_pool.py:5239](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/memory_pool.py:5239)、[index_key_cache.py:157](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/index_key_cache.py:157)、[disaggregation/utils.py:1389](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/utils.py:1389)、[disaggregation/utils.py:1543](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/utils.py:1543)。

P 在最后一个 chunk 构造完整已物化范围的 DSA page indices 作为 state payload，并交给 sender；D 根据自己的 request-to-token 表建立对应目标 page indices，且源码明确 `Indexer lives on device pool`。源码：[prefill.py:1373](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:1373)、[prefill.py:1409](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:1409)、[prefill.py:1454](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:1454)、[prefill.py:1520](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:1520)、[decode.py:1537](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:1537)、[decode.py:1655](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:1655)。

D 自己也有 prefix match 与 lock 流程，但调用的是 D 自己的 `tree_cache`，不代表 P 的目录自动包含 D 的活跃请求。源码：[decode.py:729](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:729)。

对普通 prefix lookup，本次核对的入口是本实例 tree core 与配置启用的 external linker；没有在这条普通路径看到“查询 A 正在哪个 D 解码，再从 D 的 HBM 拉回 Index K”的分支。这里是对所查路径的有限结论，不是对所有可插拔 backend 的全称断言。源码：[unified_radix_cache.py:558](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:558)。external linker 是通向外部 KV store 的配置路径，其全组件可恢复约束也不等于自动暴露任意 D 的活跃页：[unified_cache_linker.py:1](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_cache/unified_cache_linker.py:1)、[unified_cache_linker.py:67](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_cache/unified_cache_linker.py:67)。

## 4. 备份需要看具体机制，不能混为一谈

- HiCache 的已备份 host 节点可以在设备页已 eviction 后继续成为有效 match boundary；具体 write policy、sidecar 配置及成功完成条件，需要单独检查。源码：[components/full.py:105](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_cache/components/full.py:105)。
- `DSATokenToKVPool.get_cpu_copy/load_cpu_copy` 明确一起保存和恢复 KV、Index K，以及可选压缩尾部；注释说明这是防止 retraction 期间旧页重用造成恢复读错。它证明 retraction 备份必须包含 Index K，但不能用此函数单独证明“所有 prefix 都默认自动备份 DRAM”。源码：[memory_pool.py:5206](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/memory_pool.py:5206)、[memory_pool.py:5231](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/memory_pool.py:5231)。

## 5. HiCache 确实为 Index K 建立 DRAM 附属存储

在常规、具有常驻 L2 缓存的 DSA HiCache stack 中，`_DsaStrategy.build` 调用 `build_anchor_sidecar_stack`：主 KV 使用 host pool，Index K 使用 `DSAIndexerPoolHost`，并注册 `SidecarPoolSpec(pool_name=INDEXER, indices_from_pool=KV)`。这意味着附属数据跟随主 KV 的索引，不要求两个 buffer 使用相同地址。源码：[hybrid_pool_assembler.py:1931](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/hybrid_cache/hybrid_pool_assembler.py:1931)、[hybrid_pool_assembler.py:1959](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/hybrid_cache/hybrid_pool_assembler.py:1959)。

`DSAIndexerPoolHost` 的每 token 大小包含 Index K 和量化 scale，容量/page 数取自 anchor host pool；`init_kv_buffer` 按 host layout 分配实际 DRAM buffer。源码：[pool_host/dsa.py:50](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/pool_host/dsa.py:50)、[pool_host/dsa.py:152](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/pool_host/dsa.py:152)。

backup 路径构造 sidecar transfers，与主 KV 一同提交；普通 write-through 操作保持引用和 pending 状态直到完成处理。不能把 host 页已分配当成数据已传完。源码：[unified_radix_cache.py:1542](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:1542)、[unified_radix_cache.py:1590](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:1590)。

L2→HBM 恢复按层遍历该层的各项 transfer，再调用 `on_layer_done(layer_id)` 记录完成事件；Index K 读取会等待相应层的 transfer counter。因此层流水的 ready 包含该层所需的附属 buffer。源码：[l2_transfer.py:74](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/l2_transfer.py:74)、[index_key_cache.py:58](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/index_key_cache.py:58)。

备份触发时机受 write policy 影响，L2 是否作为常驻 cache 还受 host mode 影响；不能由“支持 sidecar”推导所有请求默认立即备份。官方文档说明了不同写入策略和配置：[HiCache design](https://docs.sglang.io/docs/advanced_features/hicache_design)。

## 6. Mooncake 与跨实例复用要检查完整状态

Mooncake 的 hybrid 存储键把主 KV 与 INDEXER 作为不同 component objects；`batch_exists_v2` 根据必需组件交集决定可恢复 prefix。DSA 的 ALL_PAGES sidecar 必须覆盖复用范围内每一页。源码：[mooncake_store.py:764](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/storage/mooncake_store/mooncake_store.py:764)、[mooncake_store.py:857](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/storage/mooncake_store/mooncake_store.py:857)。

存在性检查之后，实际 prefetch 同样要检查传输结果：`_page_transfer_kv_batch` 读取 KV 和所有 KV-derived sidecars，返回这些连续命中页数的最小值。`_check_hybrid_prefetch_result` 区分 DSA 的可截短安全前缀和其他窗口/状态类型。不能以主 KV 已到达代替 Index K 已到达。源码：[cache_controller.py:1115](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/managers/cache_controller.py:1115)、[unified_radix_cache.py:2328](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/mem_cache/unified_radix_cache.py:2328)。

例如主 KV 连续恢复 10 页、INDEXER 连续恢复 7 页，该 DSA 路径最多使用完整的前 7 页。该例是假设值，用于说明完整性条件，不是性能数据。

普通 HiCache 的 L1/L2 为实例私有；跨实例复用经配置的共享 L3。典型路径是生产实例备份到 L3，再由消费实例 prefetch 到本地并恢复。D 的输出也需要配置异步 offload 后才可能经此路径供 P 复用。官方依据：[层级共享范围](https://docs.sglang.io/docs/advanced_features/hicache_design#tier-sharing-scope)、[P/D 与 async offload](https://docs.sglang.io/docs/advanced_features/hicache_best_practices#integration-with-pd-disaggregation)。这不等于自动直接读取另一个活跃请求的 HBM。

## 7. HiSparse 不等于已经打通活跃 host KV 的 prefix 复用

HiSparse 在 decode 侧把完整主 KV 放 host memory，并按 sparse 选择加载所需数据；DSA 的 indexer 仍使用设备侧状态。D 的 DSA 接收路径明确从 device pool 取得 indexer 目标 indices。源码：[decode.py:1537](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:1537)。

当前 `validate_hisparse` 显式要求 `disable_radix_cache`；官方部署示例也在启用 HiSparse 的 D 实例上设置该参数。它不是一份已经提供跨请求 prefix 复用的 alive pool 实现，也不要求 P 同时关闭自己的普通缓存。源码：[hisparse_hook.py:99](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/arg_groups/hisparse_hook.py:99)；官方说明：[HiSparse guide](https://docs.sglang.io/docs/advanced_features/hisparse_guide)。

进一步核对 prefix 命中后的传输：HiSparse D 的预分配分支断言 `prefix_len == 0`，给新请求分配从起点开始的 host pages；普通无 decode prefix cache 的路径将 `total_prefix_len` 设为 0。P bootstrap 从接收侧返回的 `decode_prefix_len` 初始化 `start_send_idx`，随后通过 P 的 request-to-token 映射发送相应范围，既包括缓存命中段，也包括新计算段。P 命中减少 prefill 计算，但在这条 HiSparse 路径里不会省掉 D 所需的 prompt KV 传输。源码：[decode.py:1391](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:1391)、[decode.py:1969](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/decode.py:1969)、[prefill.py:397](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:397)、[prefill.py:1503](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/disaggregation/prefill.py:1503)。

`HiSparseCoordinator.request_finished` 等待相关 backup 后释放请求的设备热 buffer 和 host indices，并清理请求映射。因此其 host 完整 KV 和设备热数据命中都是请求内存储机制，不能等同于跨请求 prefix cache 的保留。源码：[hisparse_coordinator.py:917](/Users/hibikid/Documents/sparsekv-ascend-sglang/sglang/python/sglang/srt/managers/hisparse_coordinator.py:917)。

## 对当前设计问题的启示

原生模式首先依靠缓存对完整可恢复数据的生命周期管理：请求结束后可以继续保留设备页，或者按配置备份到其他层。当前设计主动让 compact KV 在 alive DRAM 长期可读，同时释放 P 的 HBM；这使“主 KV 仍可复用，但辅助数据可能不在同一 owner 上”的情况成为需要明确解决的额外契约。

因此，“原生也保存 Index K”支持把必要 Index K/scale 纳入可复用 payload；它本身不能替我们决定必须复制到哪一侧 DRAM。用户在后续讨论已明确接受：prefill Index K 与必要 scale 作为 alive 附属数据保存在 DRAM，供复用和 backup。附属存储的布局、副本数与写入时机仍需细化。目录只能发布已经具备完整可恢复数据及正确生命周期保证的前缀。
