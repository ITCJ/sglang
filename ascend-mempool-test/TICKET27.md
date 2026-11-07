# Ticket 27：LRU cache 与安装库接入

实现已接入当前 `cryang/dev/mempool-min`，NPU 验收待用户执行。
默认 `SGLANG_NPU_SPARSE_KV_ENABLE_LRU=0` 保留窗口替换；打开后使用 PR 的 timestamp-LRU + probation。

## 安装和配置

同事以 `ITCJ/sgl-kernel-npu` 的 `bw/pr/feat_lru_with_probation` 为基础合入
`feat/unidex-copy-multibase`。用户在 Docker 内用该库 `build.sh -a kernels`
生成 wheel，再用当前服务 Python 环境安装。SGLang 不再维护或编译独立 multibase 扩展。

安装库须同时导出：

- `unidex_copy_multibase`
- `fused_timestamp_lru_metadata_update_with_probation`
- `parallel_lru_metadata_write`
- `slot_map_lookup(..., pos_mask_size=...)`，提供第三个 hit-position mask

`SGLANG_NPU_SPARSE_KV_DEVICE_CACHE_FACTOR` 支持 1–4，默认 2。
LRU 要求 attention top-k=2048，cache 容量为 factor × 2048。
`SGLANG_NPU_SPARSE_KV_PROBATION_AGE` 默认 4；hit age 为0，新 miss age 为4。
启动脚本传递并记录三项环境变量。selected KV 仍按 attention top-k 分配。

固定预算覆盖物理 request rows（含 padding 与 decode extra slots）的 KV、slot-map、
三张 int32 LRU 表及共享初始化模板。传统 PD staging 仅在非 mempool decode 计费。
通用配置器从原始预算重新定容，避免重复扣上游 scratch，并重新做 page 对齐。
当原生配置器进一步减少 request 数时，本票预留是安全上界，不反向扩张 Index K。
临时 selected KV、copy tensors 和 Graph workspace 仍使用运行时 activation 余量。

## 正常路径与生命周期

1. manager 屏蔽无效请求、top-k、未绑定或未写入的 token，lookup 返回缓存槽与命中位置。
2. 现有四来源物化把 cache/current HBM、P/D BM 写入 selected KV。
3. refill stream 等待 hit/miss 完成，执行 victim 选择及正反映射更新，再只回填 miss。
4. attention 继续使用现有完成事件；下轮读取前等待 refill 和 metadata 完成。
5. request alloc/init 重置该请求全部 LRU 状态；pool clear 重置全表。

cache 与三张 LRU 表由 manager 持有；selected KV 属于当前 attention。
跨流临时 tensor 使用 `record_stream` 保证存活。沿用原生父进程清理，不增加 shutdown 协议。

## 用户 NPU 验收：每台容器一条命令

前置条件：两侧 checkout 为本票 commit、同一份合并库 wheel 已安装、已有可用的 mempool
双机 config.json、模型和 BM/HCCL 环境，以及之前保存的 `cases.json`（首项4096 input IDs）。
配置中填写真实 P/D 地址、网卡、模型路径、容量和端口。两侧使用同一 run ID 和 kernel commit。
组件测试使用 device 0，先确保该卡无运行服务。控制入口先 P 后 D，D 连接等待中的 P。

P：

```bash
python ascend-mempool-test/scripts/run_ticket27.py \
  --role prefill --config /path/to/config.json --cases /path/to/cases.json \
  --run-id ticket27-001 --kernel-commit <MERGED_KERNEL_COMMIT>
```

D：

```bash
python ascend-mempool-test/scripts/run_ticket27.py \
  --role decode --config /path/to/config.json --cases /path/to/cases.json \
  --run-id ticket27-001 --kernel-commit <MERGED_KERNEL_COMMIT>
```

入口自动完成库预检、组件检查、LRU off/on 两次服务启动、就绪检查、同一请求、输出比较和自身进程清理。
每个服务阶段沿用现有 `Processes` 清理并核对端口释放，成功后才推进下一阶段。
固定输出32 token，逐项比较完整 `output_ids`；observe/trace/prefix 诊断关闭。
每个配置只启动一次模型；组件不加载模型。失败返回非零，不自动重试。

报告位于 `/home/cryang/log/k2/ticket27-001/{prefill,decode}/`：
`kernel.json` 记录用户提供的 build commit、实际安装路径、版本、ABI 与 `.so` hash；
wheel 若无 commit 元信息，提供的 SHA 是构建声明，不能当作从二进制反推的事实。
`lru.json` 记录 B1 eager / B16 Graph、真实淘汰、probation、请求复用与 KV 对照。
`report.json` 汇总组件与完整 token 比较。
服务日志位于相邻 `ticket27-001-lru0/` 和 `ticket27-001-lru1/` 下，包含启动参数及清理证据。
请回传上述三个目录，用户确认后才关闭27。

加 `--components-only` 只跑 smoke，不启动双机服务、不做 token 对照。
组件使用合成 HBM 数据代替 backing sources，运行真实 manager、multibase 与 LRU 算子；
它不能证明跨机 BM 内容正确。服务对照使用实际 P/D BM 和 cache/current HBM 路径。
本票不覆盖22的跨请求合并、并发/多组或性能目标。

## 功能源码阅读顺序

Ascend/NPU：

1. `python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py`：容量与固定字节预算。
2. `python/sglang/srt/hardware_backend/npu/memory_pool_npu.py`：request 行数和原生预算适配。
3. `python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/manager.py`：状态分配、lookup、LRU、miss refill、复用重置。
4. `python/sglang/srt/hardware_backend/npu/mempool/multibase.py`：只从安装库加载算子。
5. `python/sglang/srt/hardware_backend/npu/mempool/runtime.py`：观测中记录真实容量与替换策略。

通用主线（实施前已说明必要性）：

1. `python/sglang/srt/environ.py`：统一环境变量 registry 无法在 NPU hook 内代替注册。
2. `python/sglang/srt/mem_cache/kv_cache_configurator.py`：原始预算在这里进入 Index K 定容，manager allocation hook 已太晚；仅 NPU 分支调用上述适配器。

## 脚本、测试和清理

- 新增 `scripts/run_ticket27.py`、`verify_kernel_library.py`、`verify_lru_cache.py` 和 `src/ascend_sparse/kernel_library.py`。
- 更新 `run_service.sh` 环境记录；`run_ticket26.py` 删除构建参数与调用；诊断和 Graph 入口改报告安装库身份。
- CPU 覆盖 `test_lru_budget.py`、`test_lru_manager.py`、`test_lru_integration.py`，复用既有资源/物化/复制/清理集合。
- 删除生产目录 `mempool/kernels/unidex_multibase/` 的4份源码/身份文件及 `scripts/build_multibase.py`。
- 去掉旧环境变量与 `.so` fallback；不提供旧独立库回退。当前可执行源码不再引用独立构建。
- `.scratch/` 中历史源码快照、日志和验收说明保留为历史证据，不作为当前运行入口。未访问或删除 Docker 上的历史文件。
