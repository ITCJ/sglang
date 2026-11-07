# 27: 移植 PR 的 HBM LRU cache，并使用已安装的 multibase 库

**What to build:** 在当前 mempool 四来源物化路径上接入 PR 的 timestamp-LRU + probation 策略。分离 attention top-k 与 HBM cache 容量，补齐固定 HBM 预算、manager 分配/寻址、miss 回填、元数据与请求复用重置。multibase 从用户在 Docker 安装的 `sgl_kernel_npu` 调用，运行入口不再编译独立扩展。

**Status:** needs-info

**State:** open

**Blocked by:** 用户2026-11-07明确要求开始27，允许在26未关闭时独立实施；26仍不关闭。组合kernel由同事合并；用户要求直接按已给两个branch的现有名称/接口调用，最终库身份在Docker验收时记录。

**Target:** 当前分支 `cryang/dev/mempool-min`，实施基点 `659b949ece`；依用户本轮 `/implement` 指令在当前分支提交。

## 已核对的来源

- SGLang 来源：本地 `cryang/pr/sparsity-pd-disagg-LRU`，`d8422c1d76`。参考 `3063225ff5`（合入策略）、`48412b11f1`（容量配置）、`5b4928b733`（padding 与 refill 封装）。选择所需逻辑移植，不整份替换 manager 或合并整个分支。
- kernel：已 fetch `ITCJ/sgl-kernel-npu` 的 `feat/unidex-copy-multibase` 到本地 remote ref，HEAD `fe23057`。保留原工作树及其 ahead 2 状态，未 checkout、编译、安装。
- `fe23057` 导出 `unidex_copy_multibase`，参数顺序与当前 `MultibaseCopy.launch` 一致。它没有 `fused_timestamp_lru_metadata_update_with_probation`、`parallel_lru_metadata_write`；`slot_map_lookup` 也不接受 `pos_mask_size`。
- 本地 kernel remote ref `origin/bw/pr/feat_lru_with_probation`（`e7a9cb60`）包含上述 LRU 接口；此 ref 本轮未刷新。最终 Docker 库必须同时具备两组接口。
- 上游 `build.sh -a kernels` 生成 `output/` 内的 wheel；脚本本身不执行 pip 安装。用户负责在 Docker 构建并安装最终组合库。
- 用户已决定：同事 fork [LRU kernel 分支](https://github.com/ITCJ/sgl-kernel-npu/tree/bw/pr/feat_lru_with_probation)，再合入 multibase。SGLang 以最终合并版本的实际接口为准；上述两个 SHA 仅为规划参考，不冒充交付版本。agent 不合并、不提交 kernel 库。

## 当前行为与最小移植

当前 cache 每层每请求仅保存上一轮 selected top-k，slot-map 每轮整体替换；已有 HBM 缓存，不具备 PR 的 LRU。四来源物化已合并 cache/current HBM 与 P/D BM；保留它及当前 current-token、页表、binding 和 written-prefix 有效性规则。

1. **容量与预算。** 复用 PR 的 `device_cache_capacity`：LRU 限定 top-k=2048，factor 1–4，默认 2；selected buffer 仍为 2048，物理 cache 默认 4096。HBM KV 大小为 request storage rows × capacity × KV head dim × layers × element bytes。预算覆盖新增三张 int32 LRU 表，并核对现有 slot-map/模板的固定开销；扣除一次后再计算 Index K 容量。request storage rows 包括实际 request-pool padding/适用的额外接收行，不按 Graph batch width 扩大整份 cache。
2. **manager 状态。** 分配 `device_slot_tokens`、`device_lru_slots`、`device_lru_slot_stamps`。hit 寻址的 request stride 改为 cache capacity；attention top-k、copy task 数和 selected shape 保持按 top-k。沿用现有 alloc/clear 生命周期入口重置全部状态，验证释放后重新分配不会继承旧请求的命中。
3. **算子调用。** lookup 取得 hit-position mask；LRU kernel 选择 victim；metadata-write 更新正反映射。保留 hit 的物理槽，只从 selected HBM 回填 miss。probation 默认 age=4，新取入项与已命中项优先级不同；这不是重新实现普通 Python LRU。
4. **同步与 Graph。** 保留现有 manager/attention 接口和四来源 fetch；在现有流上建立物化完成→victim/回填→下次读取的依赖。元数据使用已屏蔽的 top-k，padding/无效请求不能改变真实槽。保留跨流 tensor 存活保障。暂不移植 PR 的额外 overlap/offload_v2 重构。
5. **库与启动环境。** `multibase.load_kernel` 直接使用已安装包；清理当前生产/验收入口的独立 `.so` 构建与路径注入。复用三项环境变量：`SGLANG_NPU_SPARSE_KV_ENABLE_LRU=false`、`SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR=2`、`SGLANG_NPU_SPARSE_KV_PROBATION_AGE=4`。默认关闭，打开时采用新策略；测试入口显式传递环境。库安装与环境变量注册是两件事。

PR 的预算 helper 使用 `uses_host_kv_offload` 作为条件；不能原样移植，否则会漏掉 `PD_DECODE_MEMPOOL`。本票应覆盖 `uses_sparse_kv_cache`，同时保持 mempool 无传统 PD K/V staging 的行为。

## 预计功能源码（阅读顺序）

Ascend/NPU 专用：

1. `python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py`：容量解析及固定 HBM 预算。
2. `python/sglang/srt/hardware_backend/npu/memory_pool_npu.py`：复用 PR 的 NPU 预算适配函数。
3. `python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py`：分配、lookup、LRU、miss refill 与生命周期重置。
4. `python/sglang/srt/hardware_backend/npu/mempool/multibase.py`：改用安装库。
5. `python/sglang/srt/hardware_backend/npu/mempool/runtime.py`：若保留观测，容量和 replacement_policy 如实记录，不改变 fetch 数据路径。

通用主线（实施前已向用户单独说明，现已实施）：

1. `python/sglang/srt/environ.py`：注册三项既有 PR 环境变量。集中 registry 位于此处，NPU 模块不能独立完成同名注册。
2. `python/sglang/srt/mem_cache/kv_cache_configurator.py`：`config_from_budget` 增加受 NPU sparse 条件保护的预算调用。其掌握原始字节预算并先于 manager 分配执行；只在 NPU allocation hook 中扣除会晚于 Index K 定容。算法留在 NPU 文件，非 NPU 路径保持原行为。

最终功能源码为这七个文件。`attention.py` 原则上无需改接口。独立 kernel C++/CMake 拷贝的删除单列为旧构建清理。

脚本、测试和文档另列：复用 `ascend-mempool-test/` 的 CPU 接口与预算测试、双机服务入口、README；更新实际调用到的 kernel 准备/Graph 验证入口。历史 ticket26 日志和源码证据保留，不批量改写历史说明。

## 独立编译路径清理清单

以下清理已完成；使用两个指定分支的原导出接口，最终组合库待Docker安装验证。

- 删除 `python/sglang/srt/hardware_backend/npu/mempool/kernels/unidex_multibase/` 下的 `host.cpp`、`kernel.cpp`、`CMakeLists.txt`、`source-identity.json`，以及该独立扩展的构建产物（若存在）。本地本轮含 ignored 文件的枚举仅发现四份源码/身份文件，没有 build 产物；Docker 未检查。
- 删除 `ascend-mempool-test/scripts/build_multibase.py`。移除 `run_ticket26.py` 当前执行路径中的构建调用、仅用于构建的参数及环境注入；保留历史归档文件本身。
- 移除 `mempool/multibase.py` 的 `torch.ops.load_library` 兜底、`SGLANG_UNIDEX_MULTIBASE_LIBRARY` 与默认独立 `.so` 路径。只使用最终安装库，不留隐式回退。
- 更新 `diagnose_target_startup.py` 和 `verify_graph.py` 的旧路径处理。ABI 检查和身份报告改指实际安装包及其二进制，不再记录不存在的独立 `.so`。
- 清理只服务于旧构建的测试/夹具和当前运行文档。保留有用的 CPU copy oracle、Graph 与服务行为验证；按最终导出接口调整，不因删除编译器而删除正确性覆盖。
- 交付时全仓核对旧脚本名、环境变量、库名与源码目录引用。可执行路径不得依赖旧构建；历史日志、归档源码及历史说明的引用逐类列为保留证据，不当作待运行入口。

## 范围边界

- 这是 [22 热缓存与读合并](22-k2-w2-hot-cache-and-coalescing.md) 中容量/替换策略的窄移植。22 的跨请求 miss 合并、HiSparse 对比、容量签名/完整账本要求仍留在 22；本票不将 22 整体验收记为完成。
- 保留 26 当前只做四来源物化的范围，不恢复已经撤销的五类全面替换，不带入 25 双写或 21 预取。
- 本票预留启动 HBM 与 manager 实际分配一致；不扩展 router admission 或 prefix/DRAM 管理协议。
- 不调整 shutdown，不增加逐 rank 释放回执，不追加性能优化。代码、验收、交付说明尽量一个独立 commit。

## 验收与 implement 步骤

- [x] 核对目标分支、同事交付的最终 kernel fork/branch/commit 及 26 的实施依赖；在 Comments 记录用户决定。若用户明确允许 26 关闭前实施，单独记录该例外，不关闭 26。
- [x] 按 `/implement` 在既有接缝做精简 TDD：预算与实际 cache shape 一致；LRU 保留 hits、仅回填 misses；请求复用和 padding 不污染真实 cache。CPU 测试复用 mock kernel，不把 mock 结果当作算子正确性证据。同一测试最多 red/green 两轮。
- [x] factor=1/2/3/4 的容量/预算静态覆盖；LRU off 的旧窗口行为保留；非 mempool sparse 兼容路径不回归。无需四档都反复加载模型。
- [ ] Docker 预检同时检查 multibase、LRU 两算子与 lookup 扩展接口，记录最终库 commit/安装路径；运行入口无现场编译。
- [x] 完成上述独立编译路径清理，记录全仓残留引用分类；最终安装库缺失时不能回退到旧独立扩展。
- [ ] 用户运行一组定向 NPU 算子验证：命中、miss、真实淘汰、probation、重复请求槽使用、B1/B16 padding、Graph capture/replay 与 KV 内容一致。
- [ ] 用户运行现有双机服务入口的定向 LRU off/on 对照；固定输入，完整输出 token ID 一致，P/D BM 与 cache/current HBM 混合命中可用。沿用已经接受的模型精度口径；默认 trace/observe 关闭，定向算子报告提供淘汰证据。
- [x] 自运行入口自动准备、等就绪、请求、判定、收集报告及清理自身进程；失败非零，日志 `/home/cryang/log/`。明确 smoke 与本票完整覆盖的差别。
- [x] 实施末执行本票相关完整 CPU 集合一次、类型/静态检查，按 `/code-review` 做 Standards 与 Spec 双轴评审，交付源码分类清单并单独 commit。用户确认本票 NPU 验收后才关闭。

## Comments

### 2026-11-07：用户要求单开小票，先分析

已完成本地 PR/当前分支差异与 kernel 源码核对。用户理解的容量计算、预留、分配、算子、环境接线均属于范围；额外必需项是生命周期重置及 Graph/异步时序。尚待目标分支和组合 kernel 来源答复。

本轮仅创建规划资料并 fetch kernel remote ref；未修改功能实现，未构建或安装 kernel，未运行 CPU/NPU 测试，未提交。26/22 的验收和依赖状态保持不变。

### 2026-11-07：用户确定 kernel 由同事合并，并要求彻底清理旧编译代码

同事以 `ITCJ/sgl-kernel-npu` 的 `bw/pr/feat_lru_with_probation` 为 fork 起点合入 multibase；agent 按最终合并库接入，不执行该合并。最终 fork/branch/commit 尚未提供。

已补齐旧源码、构建脚本、加载兜底、环境注入、诊断/Graph 身份报告及当前测试文档的清理清单。只更新规划；没有删除源码、修改功能或访问 Docker。目标分支别名与26依赖保持此前记录。

### 2026-11-07：开始实施

用户明确调用 `/implement` 开始27，随后确认同事仍在合并，SGLang按此前两个branch原导出名实现，不等待新SHA。记录26验收前实施例外；27保持独立commit，用户NPU验收后才关闭。

已接入容量/预算与manager LRU、元数据重置、miss refill；精简TDD两项已观测到缺少功能时失败，green留待最终CPU集合。通用源码仅先前说明的env注册和NPU预算hook。无NPU访问。

### 2026-11-07：实现交付，等待用户执行 NPU 验收

已完成七个功能文件的最小移植、旧独立扩展源码/构建脚本/加载兜底/环境注入清理，以及启动、诊断、Graph身份记录更新。manager保留四来源物化与原attention事件接口，LRU refill stream在物化后顺序执行victim、metadata和miss refill；alloc/init/clear重置三张表。额外接收行也计入mempool cache预算，传统staging只属于非mempool PD。

Mac实际验证：新增6个测试全部通过。一次完整CPU集合649项，修复1处新测试装载器后仅复跑受影响25项（含权限受限的24项），全部通过；另补1项普通host-offload LRU回归通过。共650项中643项通过，余下7个测试方法在只读实施基点659b949ece也复现同样失败（含subtest共8 failures/6 errors）。未修无关基线问题，未重复全量；任何同一测试方法不超过2轮。

类型检查：8个相关源码/入口通过（忽略Mac缺失的外部SDK imports）；较宽mempool严格检查仍有未安装triton/sgl_kernel_npu及既有平台/类型错误，未声称全仓类型全绿。相关Ruff、Python语法、shell语法和diff空白检查通过。详见[本地检查记录](../verification/ticket27-local/checks.json)。

/code-review两轴：Standards发现1项组件可能导入旧checkout，已修复并静态复核，最终0未解决；Spec同一项已复核修复，最终0未解决。组件进程固定当前repo/python并报告源HEAD。

[用户运行说明与源码阅读清单](../../../ascend-mempool-test/TICKET27.md)提供每台一条命令：安装库预检、B1 eager/B16 Graph组件、LRU off/on完整输出token对照，复用现有Processes/端口清理，日志在/home/cryang/log/k2/。--components-only明确为smoke。未访问NPU、未安装或合并kernel，实际最终库commit由运行参数记录，ABI/hash来自安装包；用户NPU验收仍全部待办。27/26保持open，22其余范围未完成。

### 2026-11-07：001 NPU组件失败与捕获stream修复

31回传verify_lru_cache在torch.npu.graph.__enter__报“must be captured on a non-default stream”。此前B1 eager步骤已走完；B16尚未开始capture，双机服务对照未运行。末尾internal-format warning不是此次异常。

仅修改组件入口：独立capture stream先等待输入准备stream，step接收capture stream，捕获后caller等待该stream。生产manager/kernel不变。新增公开capture helper回归两轮red/green复现同一错误并通过，单文件mypy和diff检查通过。实际NPU修复效果待用户复测，27仍open。

下一轮先单独运行verify_lru_cache.py，报告使用ticket27-002，保留001日志；不重启模型、不自动重跑完整服务。
