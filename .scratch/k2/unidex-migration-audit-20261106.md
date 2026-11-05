# UniDexCopy 迁移范围复核（2026-11-06）

**后续确认：**计划侧已批准[26统一迁移](issues/26-unidex-multibase-backend.md)，下文为建票前的调用链基线。
多DRAM一次读为必需项，两路HBM融合可暂缓；保留的旧核兼容分支也全部接入新后端。
下文关于14微测写路径“不改”的排除项现仅说明历史代码：新增kernel测量由26承接，旧SDK成绩保留对照。
生产fill/admit的SDMA路径仍不变。细节以[26方案](ticket26-plan.md)为准。

本次重新沿配置、上游调用者、分支条件和最终提交点核对。之前将源码中保留的 fill/admit 核分支列在当前路径的迁移表中，没有区分是否执行；原生 Host offload/get_forward_kv 也被列得过宽。本文件修正这些范围，作为后续迁移清单的依据。

**当前必迁移的是5类操作、7处生产提交点：主KV/Index K写回、四来源swap-in、HBM回填、slot-map、prefix load_back的gather。当前fill/admit继续走SDMA，排除在核替换之外。**

复核基线：本地 HEAD `aa5852624e`，生产源码无未提交改动；mempool P/D、共享布局、router页分配、P eager / D Graph、`pool_copy=true`。本次只检查本地源码与已接受配置的生成入口，未连接设备，也未取得新的运行trace。Graph“执行”指捕获后的设备节点执行，不是每次replay重跑Python方法。

## 1. 配置与实际路径依据

- [run_k1_full_acceptance.py:21][config-runner] 的 `configurations()` 明确设置 `prefill_shared/decode_shared/prefill_index_shared/prefix_shared/match_prefix/pool_copy=True`；18沿用此入口。`--page-allocation`控制18验收步骤，不是生产是否创建页表的开关；生产在router存在时调用 `enable_page_tables`。[runtime.py:1426][runtime-tables]
- [RouterConfig:51][router-config] 的一般默认仍为 `pool_copy=False`。因此“当前fill/admit是SDMA”基于上述已采用配置，不能扩大为所有配置都没有核分支。
- [runtime.py:1388][executor-install] 在P、prefix启用、`router.pool_copy`为true时创建 `PoolCopyExecutor(manager.pool_transfer())`，传入 `PrefixStore`。
- P是 `PD_PREFILL_MEMPOOL`，不启用sparse manager；D是 `PD_DECODE_MEMPOOL`，启用sparse manager，但不启用原生Host KV offload。[模式选择与属性][sparse-config]

## 2. 当前服务必迁移清单

| 业务操作 | 实际上游调用与UniDexCopy提交点 | 迁移判断 |
|---|---|---|
| P主KV→P alive；D新KV→D alive | `ascend_backend` prefill/decode → `runtime.write_layer` → `MempoolKVOffload.write` → [offload.py:117][offload] | 改multibase 1→1；保留P按页、D按feature/layer的写者分工 |
| P Index K→P alive_index | `runtime.write_layer` → `_index_writers[layer].write`，最终也是 [offload.py:117][offload] | 同一提交点，按Index K行宽处理；不是admit |
| D HBM cache hit→selected | sparse attention → `materialize_selected_kv` → [sparse manager.py:1022][hit] | 纳入多源swap-in合并 |
| D当前token的HBM暂存→selected | `runtime.fetch_selected_kv` → `DecodeKV.supply_current` → [decode_write.py:80][current] | 纳入合并；仍优先从生产者HBM直供，不能改读尚未对共享读者安全的当前DRAM行 |
| P远端alive＋D本地alive→selected | `runtime.fetch_selected_kv` → `KVFetch.gather` → `SparseKVCopy.gather` → [copy.py:155][fetch] | 同一代码点循环提交两次；改成多基址路由，并与hit/current合并 |
| selected→HBM cache回填 | `materialize_selected_kv` → [sparse manager.py:1079][refill] | 改multibase 1→1；保留gather完成后的独立回填 |
| top-k槽值→slot-map | `materialize_selected_kv` → [sparse manager.py:1118][slotmap] | 改multibase 1→1；行宽4B；保留独立调用与顺序 |
| P prefix主KV/Index K→load_back临时HBM | SR `load_back` → `NativePrefillRestore.load_back` → `PrefixStore._submit_load_back` → [prefix.py:868][loadback] | gather改multibase；同层同类型的多个段可合并；后续all_gather和native/NZ scatter保留 |

前两行共享同一提交点，BM两来源也共享一个循环提交点，合计7处不同的代码位置。不要把7处位置误算成每层7种独立业务。

P/D写回的实际入口是 [ascend_backend.py:1357][backend-prefill] / [2819][backend-decode] → [runtime.py:646][write-main]，P Index K在 [runtime.py:683][write-index]；不是原生 `SparseKVCacheManager.offload_v2`。

swap-in当前有四个地址来源：HBM cache、当前token HBM、P远端BM、D本地BM。后三路由 [runtime.py:780][fetch-runtime] 接入；current写入后从miss mask中去掉。合并时必须保留这个区别。当前物化为四次copy；refill和slot-map各一次；逐层D写回又是另外一次。减少调用的范围是物化四次合成一次，不能把它说成删掉写回或SDMA。

`load_back`在控制操作流上提交，不属于逐步D decode Graph。`_copy_complete`明确令load_back查询设备事件，即使开启pool_copy也如此。[prefix.py:371][completion]

## 3. fill/admit为什么不在当前核迁移范围

实际调用链：

```text
SR fill/admit
  → PrefixStore.fill/admit
  → _submit_pool_copy
  → CopyTask（整页GVA块）
  → PoolCopyExecutor独立线程
  → BMTransfer.copy_data_batch(G2G, flags=1)
  → 同线程wait()
  → READY → tick poll → receipt
```

- admit分支在 [prefix.py:312][admit-branch] 选择执行器；fill分支在 [prefix.py:545][fill-branch] 选择执行器。
- [prefix.py:384][plan-sdk] 按page-first整页建块，`CopyBlock`使用SDK GVA；没有调用 `copy_rows`。
- [pool_copy.py:61][sdk-transfer] 明确使用 `BmCopyType.G2G`，然后 `handle.wait()`。只有transfer返回后，执行线程才产生READY。[pool_copy.py:185][sdk-ready]
- `load_back`则在同一分派处进入 `_submit_load_back`，所以“prefix.py还有UniDexCopy”不代表fill/admit仍用它。

这条SDMA路径、GVA块计划、完成轮询、错误传播和receipt不因multibase迁移而改变。25开启后取消fill拷贝、admit只publish，是下一票的业务变化。

## 4. 当前未执行的分支：单独处理兼容，不能计作当前迁移收益

| 代码位置 | 可达条件 / 上游证据 | 处理建议 |
|---|---|---|
| [sparse manager.py:748][host-write] `offload_v2` | [attention.py:71][host-write-gate] 要求 `uses_host_kv_offload`；两个mempool模式均不满足 | 保留非mempool功能时，通过统一封装做ABI适配；不计入当前P/D写回 |
| [sparse manager.py:859][host-full-read] `get_forward_kv` | [attention.py:315][host-full-gate] 仅sparse prefill/extend；P mempool不用sparse manager，D mempool拒绝非decode | 是原生Host全量取回，不是mempool prefix load_back；作为非mempool兼容项 |
| [sparse manager.py:1054][host-miss] Host miss→selected | 位于 `mempool_runtime is None` 的else分支；当前走 `runtime.fetch_selected_kv` | 无mempool时的Host读，保留时随公共物化接口兼容 |
| [prefix.py:1012][old-prefix-copy] `_copy_region` | 仅由 `_submit_fill` / `_submit`调用；二者在copy_executor不存在时执行 | 仅 `pool_copy=false` 的旧核路径。不要为了迁移使当前配置切回此分支 |
| [sparse manager.py:677][old-offload] `offload` v1 | 当前仓库未找到生产调用者；独立资源验证只检查mempool模式拒绝调用 | 不列当前必改路径；可随公共封装兼容，或在实施时单独清理确认无用代码 |

用户希望统一保留的单源与多源调用，这个目标仍可成立：保留的兼容分支也使用同一底层multibase封装，但必须保持各自模式条件，不引入SDMA→kernel的回退。尤其 `PrefixStore._copy` 同时供load_back和旧核分支使用，统一它可能让旧分支自然兼容；这不是当前fill/admit传输方案的改造。

`SparseKVCacheManager`共7处直接调用：当前活跃3处（1022/1079/1118），非mempool3处（748/859/1054），无生产调用者1处（677）。mempool模块另有5处数据提交点，其中4处当前活跃（offload/copy/decode_write/load_back）、1处仅旧分支（_copy_region）。这12处已逐一对照调用条件。

## 5. 不改的操作与测试范围

明确排除：

- 当前fill/admit的SDK G2G batch/wait执行器。
- load_back的all_gather与 `restore_kernel.scatter_rows` 的native/NZ布局恢复。
- `slot_map_lookup` 查询算子；迁移的是slot-map写入copy。
- `DecodeKV.prepare` 的HBM `index_copy_`、页表内容更新以及原生Index K传输；这些不是UniDexCopy替换点。
- ticket14 `bandwidth_device.Device.transfer` 的L2G batch/wait。其读测量和写后核对才调用UniDexCopy。[bandwidth_device.py:95][bench-sdk]

需跟随公共接口适配的验证代码：

| 文件 | 实际职责 |
|---|---|
| `scripts/verify_graph.py` | 旧12参数schema检查；复用生产SparseKVCopy建立Graph |
| `scripts/verify_fetch.py`、`verify_page_first_copy.py` | 直接旧ABI或page-first复制验证 |
| `src/ascend_mempool/sharing_copy.py`、`sharing_pipeline.py`、`cd_copy.py` | 独立共享/混合读写工作负载，不是服务控制路径 |
| `scripts/verify_pool_sdma.py:225` | SDMA结果的UniDexCopy读回核对；SDMA被测传输本身不改 |
| `src/ascend_mempool/bandwidth_device.py:193/324` | 带宽读操作与写结果读回；L2G写入不改 |
| `scripts/verify_resources.py:125/126` | 原生 `offload_v2/get_forward_kv` 资源验证；属于兼容覆盖 |
| `tests/unit/`中的核注入替身 | 跟随所覆盖的公共调用边界适配；不扩充重复初始化或额外模型轮次 |

`ascend_mempool/__init__.py`通过包路径加载生产mempool模块，没有第二套需要同步修改的copy/offload实现。[测试导入桥][test-import]

## 6. 本轮修正与下一步边界

已修正旧研究报告和选型摘要的“全部调用”表述，明确当前必改、条件兼容和SDMA保留。核选型仍为通用多基址 `binary-v1`；本轮没有改kernel选型或票据依赖。

如果后续实施统一迁移，核心范围是上方5类当前操作和保留兼容入口的ABI接通；fill/admit SDK路径保持。ticket25才改变写目标和publish，ticket20才改变chunk行为。本次未创建ticket、未修改功能源码、未执行CPU/NPU功能测试。

本次验证为配置/调用链静态复核、全仓库UniDex引用检索，以及另一研究agent对原生manager分支的独立交叉核对。没有以静态可达性冒充实机覆盖。

[config-runner]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/ascend-mempool-test/scripts/run_k1_full_acceptance.py:21
[router-config]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py:51
[runtime-tables]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:1426
[executor-install]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:1388
[sparse-config]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py:57
[backend-prefill]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py:1357
[backend-decode]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/attention/ascend_backend.py:2819
[write-main]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:646
[write-index]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:683
[offload]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/offload.py:117
[hit]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1022
[current]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/decode_write.py:80
[fetch]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/copy.py:155
[fetch-runtime]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/runtime.py:780
[refill]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1079
[slotmap]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1118
[loadback]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:868
[completion]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:371
[admit-branch]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:312
[fill-branch]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:545
[plan-sdk]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:384
[sdk-transfer]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/pool_copy.py:61
[sdk-ready]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/pool_copy.py:185
[host-write]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:748
[host-write-gate]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/attention.py:71
[host-full-read]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:859
[host-full-gate]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/attention.py:315
[host-miss]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:1054
[old-prefix-copy]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/mempool/prefix.py:1012
[old-offload]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py:677
[bench-sdk]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/ascend-mempool-test/src/ascend_mempool/bandwidth_device.py:95
[test-import]: /Users/hibikid/Documents/sparsekv-ascend-sglang/sglang-mempool-min/ascend-mempool-test/src/ascend_mempool/__init__.py:1
