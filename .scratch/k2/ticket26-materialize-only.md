---
title: Ticket26：只保留四来源物化
subtitle: 交接 hybrid 对照、回退边界与单请求性能验证
template: doc
---

## A 结论与范围

交接 hybrid 与当前版都用全局行号、基址区间和 mask 完成多来源物化。
两份插件 host.cpp、kernel.cpp 逐字相同。
上层任务组织、地址表和复制路径不同。
静态差异不能直接解释性能退化。

本轮只保留 P DRAM、D DRAM、HBM cache、HBM current 到 selected KV 的一次 multibase。
其余生产代码恢复 aa5852624e15df139df7c76bb8d757341b049713。
当前广泛替换版分析基点为 93a1d9a7c9。
硬件由用户运行，baseline 不再重跑。

## B 来源与优先级

交接文件来自 unidex-kv-handoff-20261105.zip 内：
binary-v1/tests/manual/unidex_copy/native-vs-hybrid-20261105/graph-runtime/。

hybrid.manager.py:738–788 先算 cache hit，再算互斥 host miss。
没有独立 current-token 来源，当前 token 沿普通 hit/miss 路径读取。
hybrid.ops.py:171–230 把两个源放进连续全局行号区间。
Host 地址必须是已注册的 NPU 可见地址。

当前 runtime.py:784–815 中，cache hit 先排除 miss。
最新 token 仅在 miss 时从本 rank 的 current HBM 读取。
current 从剩余 miss 中剔除；其余由 prompt 长度划分 P/D。
所以逻辑优先级是 cache hit、current miss、普通 P/D miss。
并非无条件用 current 覆盖已有 cache hit。

当前 copy.py:93–118 建立 12 源槽：P、D、8 个 prefix、cache、current。
copy.py:183–218 读取 PageLocations，支持正副本选择与失效回退。
四来源物化无需这些未来来源槽。
保留 baseline 的 MempoolKVView.map_rows 即可解析分散物理页。

## C 任务表与 Graph

令 N=B×K，B 包含 Graph padding 行。

| 项目 | 交接 hybrid | 广泛替换版 |
|---|---|---|
| 任务槽数 | 2N，hit 与 miss 各一组 | N，每个 selected 位置选一个来源 |
| 索引 | miss 加 device_rows，再拼接 | 页映射后加源区间起点，逐源 where |
| mask | 两组互斥 mask 拼接 | 合并有效路由；过滤无效页及越界物理行 |
| 地址表 | 2 源、1 目标 | 12 源、1 目标 |
| 捕获初始化 | pinned 描述符→nonblocking H2D | device empty→逐字段 fill_ |
| 每次物化任务更新 | Add、3 Cat 等设备操作 | 路由计算、3 个任务缓冲 copy_ |

交接 hybrid.ops.py:154–230 在 capture 时绕过 eager 描述符缓存。
描述符保存在 _capture_descriptors，活到服务结束。
该分支中的 H2D 也是捕获路径的一部分。
不能声称 Graph 回放没有元数据传输。

当前 multibase.py:72–147 以设备 fill 初始化表。
若创建发生在 capture 中，这些 fill 也会进入图。
已有计划仍在回放中执行任务 copy_，读取动态选择结果。
IndexedCopy:226–227 还有每次调用的指针 fill，但它属于非物化适配。
本轮删除该适配，不把这些更新混算进物化代码。

B=16、K=2048 时，交接为 65,536 个任务槽，当前为 32,768。
槽数包含无效 padding，不等于实际搬运行数。
本轮保留当前 N 槽选源方式，不再引入任务生成优化。

## D Stream 与存储

交接 hybrid.manager.py:795–858：
主 stream 生成索引并记录 copy_ready。
独立物化 stream 等待它，执行 multibase，记录 materialize_done。
attention 与原生 refill 等物化完成；slot-map 等 copy_ready 后并行执行。
attention 最终沿原路径等待 refill/slot-map。

广泛替换版稀疏 manager.py:1054–1244：
融合执行在原 miss stream，hit stream 仅记录兼容事件。
attention/refill 仍等 hit_done 与 miss_done。
slot-map 可与物化并行，结束前按原依赖汇合。

合并后 HBM hit 不再独立于 DRAM miss stream 执行。
这是物化合并本身的调度变化，不能当作保留并行度不变。

交接物化 block_dim=48；原生 refill=24、slot-map=48。
当前物化同为48，BF16 576列每行1152字节。
kernel 以 ceil(N/block_dim) 分配连续任务槽。
每核解析区间，使用两槽 FIFO 搬运行；插件保持不改。

交接每次 torch.zeros 分配 selected；显式记录 raw-pointer 数据的 stream。
广泛替换版按 batch shape 复用 selected arena。

本轮恢复 baseline 分配方式；保留 capture copier 对输出及地址表的引用。
新增跨流消费者只为融合输入记录 record_stream。
refill/slot-map 额外观察索引和生命周期补丁随观察扩展撤销。

## E 保留与回退清单

| 差异 | 本轮处理 | 必要性 |
|---|---|---|
| 四来源选源、互斥 mask | 保留 | 合并四次复制 |
| BM 注册地址与物理页 stride | 保留 | BM 无法按普通 Host tensor 行寻址 |
| 四源地址表、任务表与 capture 存活 | 保留 | raw-pointer kernel 与 Graph 依赖 |
| miss stream 的输入就绪和完成事件 | 保留 | 不提前读当前 KV，不提前消费 selected |
| 12 源预留及正副本位置表 | 撤销 | 四来源不需要未来复制路由 |
| TensorCopy/IndexedCopy | 删除 | 写回、refill、slot-map 恢复原生 |
| D KV、P KV/Index K、prefix 搬运 | baseline 原文 | 不属于物化合并 |
| selected arena 复用 | 撤销 | 恢复 baseline 分配策略 |
| 新观察字段、预热诊断 hook | 撤销 | 不属于物化合并 |
| 通用模型、attention、shutdown | 不新增修改 | 无需改动主线 |

旧12源Graph微测的候选位置数据仅作为测试夹具保留。
它不再作为生产 manager 的状态，也不参与本轮性能运行。
旧报告与离线读取入口保留，移除依赖已删生产功能的测试。

## F 性能证据与下一轮

| 指标 | 026 baseline | 027 广泛替换 |
|---|---:|---:|
| Router TTFT ms | 14189.557 | 14300.690 |
| TPOT ms/token | 452.108 | 1236.298 |
| Decode token/s | 2.211862 | 0.808866 |
| 客户端 E2E ms | 36436.437 | 61593.909 |

用户回传两轮均为单请求测量，不能归因单个 kernel。
下一轮沿用测量入口和同一4096输入、32输出请求。
P eager、D Graph；observe、trace、额外内容核对关闭。
资源清理由用户确认，不重跑 baseline，不自动推进下一轮。

60项不同CPU测试已通过，仅修正后重复1项。
首轮新测试误将无效cache槽要求为零，按原生mask语义修正。
另修复sharing微测的读写ABI断接，14项检查通过。

Ruff、源码范围与逐字比较通过。
Standards / Spec各0项未解决。
NPU 尚未验证；26保持open，25不解锁。

## G 最终源码阅读清单

以下路径均相对 python/sglang/srt/hardware_backend/npu/。
通用主线功能源码：无改动。默认shutdown保持不变。

| 保留的 Ascend/NPU 文件 | 用途 |
|---|---|
| sparsity_driven_kv_offload/manager.py | cache hit转入融合；保留原事件与跨流存活 |
| mempool/runtime.py | 仅fetch_selected_kv增加cache/current路由 |
| mempool/copy.py | baseline页映射、四源表、N任务、capture引用 |
| mempool/multibase.py | 表分配、元数据更新、一次kernel调用 |
| mempool/kernels/unidex_multibase/host.cpp | 原交接ABI、调度和元数据stream引用 |
| mempool/kernels/unidex_multibase/kernel.cpp | 原交接多基址解析和搬运算法 |

CMakeLists.txt与source-identity.json保留原编译配置和来源记录。
插件host/kernel与交接包逐字相同，本轮未改kernel。

| 恢复 baseline 原文的文件 | 恢复内容 |
|---|---|
| mempool/decode_write.py | current缓存及原生复制入口 |
| mempool/offload.py | P/D KV、Index K原生写入 |
| mempool/indexed_copy.py | 原生索引复制、无多目标适配 |
| mempool/prefix.py | prefix原生搬运 |
| mempool/manager.py | 原页表，无正副本位置扩展 |
| mempool/observation_device.py | baseline观测，无新增cache检查 |
| sparsity_driven_kv_offload/attention.py | selected逐次分配、原attention路径 |
| graph_runner/npu_cudagraph_backend.py | 原Graph预热，无index诊断hook |

runtime其余28个方法、稀疏manager其余22个方法与baseline一致。
稀疏manager的refill/slot-map代码尾段也逐字一致。
删除page_locations.py、index_diagnosis.py，以及TensorCopy/IndexedCopy。

脚本与测试另列：

- diagnose_target_startup.py：保留单请求测量，去掉已撤销的预热hook等待。
- measure_mempool_router.py：未改，沿用026/027计时口径。
- prepare_migration_sample.py：沿用隔离准备；只澄清manifest的比较边界。
- diagnose_index_add.py：删除，原探针依赖已撤销的观察接口。
- sharing_copy、cd_copy及page/SDMA验证：写侧原生，物化读侧使用独立ABI。
- multibase_fixture.py：仅独立微测使用的候选拓扑和单源适配。
- multibase_graph、bandwidth_device：引用测试夹具，不扩展生产manager。
- ascend_sparse夹具：恢复baseline，移除TensorCopy和selected arena状态。
- CPU测试：四源优先级、单次调用、原生refill/map、散页、绑定复用、共享current。
- 测量测试：相同请求、关闭观察、SSE口径与清理；旧日志解析仍可离线使用。

本轮不重跑旧组件验收、12源微测或完整V1–V10。
旧NPU报告是历史版本证据，不替代这次回退的硬件验证。

## H 028单请求运行

用户先确认027两侧进程结束、设备资源可用。
新目录为 /home/cryang/log/k2/k2-ticket26-028-materialize-perf。

prepare自动建目录；cmp确认请求与026完全相同。
31先执行，32随后执行；每侧是一条命令链。
不加legacy-kernel，不启动observe或trace，不做baseline重测。

31容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 ascend-mempool-test/scripts/prepare_migration_sample.py \
  --variant phase2 \
  --root /home/cryang/log/k2/k2-ticket26-028-materialize-perf \
  --config /home/cryang/log/k2/k2-ticket26-026-baseline-perf/config.json \
  --cases /home/cryang/log/k2/k2-ticket26-015-reference/prefill/cases.json &&
cmp /home/cryang/log/k2/k2-ticket26-028-materialize-perf/request.json \
    /home/cryang/log/k2/k2-ticket26-026-baseline-perf/request.json &&
cd /home/cryang/log/k2/k2-ticket26-028-materialize-perf/source &&
python3 ascend-mempool-test/scripts/build_multibase.py \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --cann /usr/local/Ascend/cann-9.0.0 &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role prefill \
  --config /home/cryang/log/k2/k2-ticket26-028-materialize-perf/config.json \
  --run-id k2-ticket26-028-materialize-perf \
  --sample-json /home/cryang/log/k2/k2-ticket26-028-materialize-perf/request.json \
  --without-index-sync
```

32容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 ascend-mempool-test/scripts/prepare_migration_sample.py \
  --variant phase2 \
  --root /home/cryang/log/k2/k2-ticket26-028-materialize-perf \
  --config /home/cryang/log/k2/k2-ticket26-026-baseline-perf/config.json \
  --cases /home/cryang/log/k2/k2-ticket26-015-reference/decode/cases.json &&
cmp /home/cryang/log/k2/k2-ticket26-028-materialize-perf/request.json \
    /home/cryang/log/k2/k2-ticket26-026-baseline-perf/request.json &&
cd /home/cryang/log/k2/k2-ticket26-028-materialize-perf/source &&
python3 ascend-mempool-test/scripts/build_multibase.py \
  --sgl-source /home/cryang/sgl-kernel-npu \
  --cann /usr/local/Ascend/cann-9.0.0 &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role decode \
  --config /home/cryang/log/k2/k2-ticket26-028-materialize-perf/config.json \
  --run-id k2-ticket26-028-materialize-perf \
  --sample-json /home/cryang/log/k2/k2-ticket26-028-materialize-perf/request.json \
  --without-index-sync
```

两侧应输出PERFORMANCE_MEASURED后退出。
回传performance JSON及各侧summary.json中的status/cleanup。
成功只表示一个请求测量完成，不代表性能已恢复。
失败保留对应launcher.log；不自动重试或推进下一轮。
