# Ticket03 S3：按模式停用旧 host KV 和 main-KV staging

2026-10-04。用户授权“commit 当前修改，然后实现 ticket03 s3”。先将 S2 gate 参数修正、
回归及用户 NPU 验收记录提交为 `dd1f92f618`；本次 S3 以该提交为基线。
S3 代码和 Mac 检查已完成，等待用户执行 NPU 资源验收。
[Ticket03](issues/03-prefill-direct-offload.md) 保持 `State: open`，S4–S6 尚未完成。

## 本次行为

正式 D 构造 `SparseKVCacheManager` 时保留 HBM sparse cache、slot map、hit/miss/refill
所需 stream/event 和请求 hooks。旧 `host_kv_buffer`、SHM host/device 映射、
`host_kv_ctx_len` 及 PD staging copy stream 不再分配；三个 PD host-copy metadata
字典保留为空容器，不记录正式请求，用于保持统一 cleanup 入口。

attention 按 `uses_host_kv_offload` 决定是否调用 `offload_v2()`。正式 D 即使
`save_kv_cache=True` 也跳过旧 host 写入；temporary KV 仍由 backend 已有
`mempool_runtime.write_layer()` 写入 D BM，S2 的 materialization 继续处理 HBM hit
与 P/D BM miss，再供 attention 使用。正式模式调用旧 host 写入、host prefix 读取或
staging 接口时明确报错。正式 attention 仍只支持 decode，extend 等组合明确拒绝。

P native HBM K/V 继续分配供 prefill 计算；D 保留 Index K native pages 和 allocator。
`AscendKVManager` 沿用 S1 的 `uses_pd_decode_staging` 分支：正式 D 不创建 staging pool，
receiver 直接保留 native page indices，Success 不执行 staging→host copy。
普通/shadow 路径继续分配 staging、重写 main-KV 目标索引，并在 Success 后写入 host。
原生 transfer worker 的完成、失败、abort drain 与 service 延迟 native 回收保持原合同。

新请求经 `alloc` 复用 row 时清除旧 slot map；持有原 row 的续跑 chunk 保留 cache。
`init_req` 在 host metadata 不存在时仍能 reset。`free` 清理 metadata 并调用原 allocator；
`clear` 清空所有 slot map、存在的 host length metadata，再调用原 clear。
cache 数据本身不需要清零：没有有效映射就不能作为 hit 使用。

## 资源构造与服务启动的边界

本次允许显式构造正式模式的 native pool 和 sparse manager，用真实分配验证 S3。
ModelRunner 的早期校验、attention backend 和 BM runtime 工厂仍拒绝正式服务启动。
native pool 的 `get_contiguous_buf_infos()` 也保留正式模式保护，报错提示 S4–S5。
这样 P 虽然保留 native K/V，也不会提前发布错误的正式传输清单。

当前 `SGLANG_NPU_ENABLE_MEMPOOL=1` 仍选择 shadow，运行它仍会看到旧 host/staging。
S3 gate 通过显式组件 mode 验证正式资源，不修改服务开关。
Index K 专用 PD 发布/配对/发送合同属于 S4；正式服务 Graph、同步与生命周期属于 S5。
本阶段没有启动正式 PD 服务，也没有将“main-KV 发送为零”记为已验收。

## 修改路径与功能

生产代码路径相对 `python/sglang/srt/`；本次没有新增生产模块。

| 路径 | 功能 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/manager.py` | 根据 mode 实际跳过 SHM、指针映射、host length tensor、staging copy stream；限制旧 host/staging 入口；正式请求不记录 host-copy metadata；修正 init/clear hooks。 |
| `hardware_backend/npu/sparsity_driven_kv_offload/attention.py` | `save_kv_cache` 与 `uses_host_kv_offload` 同时满足才走旧 offload；正式 D 使用已有 BM writer/fetch 链路。 |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | 更新正式服务启动保护说明为 S4–S5；不改变开关解析结果。 |
| `hardware_backend/npu/memory_pool_npu.py` | 允许独立构造正式 P/D 资源；正式 PD buffer 发布仍拒绝，普通/shadow staging 注册分支保留。 |
| `disaggregation/ascend/sparse_pd.py` | staging pool 的直接构造入口检查 mode，在分配 K/V staging 之前拒绝不适用模式。 |

| 类型 | 路径 | 功能 |
| --- | --- | --- |
| 新增 | `ascend-mempool-test/scripts/verify_resources.py` | 单机 NPU gate：真实 native/sparse pool、allocator、Index K 写读、资源字节数、row 复用、普通 host roundtrip 与 PD adapter 分支。 |
| 新增 | `ascend-mempool-test/tests/unit/test_sparse_resources.py` | CPU 下执行真实 sparse manager 构造、旧入口拒绝、请求 hooks、普通/shadow staging 与 host 读写；检查实际 PD adapter 方法及 gate 的数据 oracle。 |
| 修改 | `ascend-mempool-test/tests/unit/test_materialize.py` | 正式 attention 测试使用 `save_kv_cache=True`，防止默认参数重新触发 host 写入；补齐 CPU stream 边界并隔离 attention SDK 替身。 |
| 修改 | `ascend-mempool-test/tests/unit/test_sparse_config.py` | 正式启动保护仍存在，提示后续 S4–S5。 |
| 修改 | `test/registered/unit/npu/test_sparsity_driven_kv_offload_config.py` | 真实 native pool 构造矩阵增加正式 P/D，检查 P 保留 K/V、D 仅保留 Index K。 |
| 修改 | `ascend-mempool-test/README.md` | 加入 S3 gate 前置条件、命令、资源矩阵、失败判据和证据边界。 |
| 修改 | `python/sglang/srt/hardware_backend/npu/mempool/README.md` | 更新正式模式的资源状态和服务启动保护边界。 |
| 修改 | `.scratch/ascend-mempool/issues/03-prefill-direct-offload.md` | 更新 S3 进度、检查结果和待执行验收，保持整票 open。 |
| 新增 | `.scratch/ascend-mempool/ticket-03-s3-summary.md` | 本阶段实现、文件清单与用户验收交付。 |

## Mac 实际执行的检查

先将正式 attention 用例的 `save_kv_cache` 改为 True，复现旧 host 写入报错；修正分支后
通过。随后用构造和请求复用用例复现正式构造被旧 guard 拒绝、host metadata 缺席时
`init_req` 报错，逐项修正。普通模式执行真实 offload / get_forward_kv，校验 KV 内容。

```bash
PYTHONPATH=ascend-mempool-test/src /private/tmp/ascend-mempool-s1/bin/python -B -m unittest discover -s ascend-mempool-test/tests/unit -q
/private/tmp/ascend-mempool-s1/bin/mypy --config-file ascend-mempool-test/pyproject.toml python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py python/sglang/srt/hardware_backend/npu/mempool ascend-mempool-test/src ascend-mempool-test/scripts
```

结果：独立 CPU suite **176 项通过**；mypy 覆盖的配置/BM/独立脚本 **27 个源文件通过**；
本次 10 个 Python 文件的 Ruff lint/format、新文件 isort 检查通过。
故障注入用例中的 FAIL/fault 日志是预期输出，最终 unittest 为 OK。

CPU 构造测试替换 NPU SDK、设备 stream、serving native pool 类型和 allocator 边界；
实际 sparse 分配、host-copy、hooks 与 materialization 均执行生产代码。
PD adapter 沿用已有 `test_native_release.load_methods`，执行原方法体，替换父 transport；
因此不等于执行完整 SGLang import/构造链。NPU gate 使用实际完整模块及构造器补足这一项。

本机尝试执行 registered 配置测试，但在 SGLang 导入时因 Python 3.9 不支持
`str | None` 报错，测试未运行，不能记为通过。完整 SGLang suite 和 NPU gate 尚未执行。
在用户现有 Python 3.11 SGLang 环境运行该回归：

```bash
PYTHONPATH=python python3 -m unittest test/registered/unit/npu/test_sparsity_driven_kv_offload_config.py -v
```

## Standards

以 `dd1f92f618` 为固定基线，规范审查覆盖全部改动和新增文件：无发现。
资源策略继续由 mode 派生；旧路径保留，正式服务保护及硬件验收状态明确。

## Spec

规格审查发现并修复一项 P2：NPU gate 在默认 stream 写入 staging 后，需要先等待
写入完成，再注入 `KVPoll.Success`，让 adapter 的独立 copy stream 安全读取。
已在 seed 后加入 `torch.npu.synchronize()`；复查确认无剩余规格问题。
修正后定向 CPU 回归 14 项通过，脚本 Ruff lint/format 与 mypy 检查通过。
该同步的实际 NPU 执行仍包含在下述用户验收中。

## 用户 NPU 验证

任选一台已安装当前 checkout、torch_npu 和 sgl_kernel_npu 的机器，使用空闲 NPU。
沿用 S2 的 CANN/torch_npu 环境。无需模型权重、P/D 启动顺序、BM peer、NIC 或端口。
默认 device 0；每个 mode 独立进程，避免共享 SHM 名称和全局 manager 干扰。
不要使用 Python `-O` / `PYTHONOPTIMIZE`。

仓库根目录执行：

```bash
bash -o pipefail <<'SH'
set -eu
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
git rev-parse HEAD > /tmp/ticket03-s3-version.txt
for mode in pd_prefill_mempool pd_decode_mempool local_offload pd_decode_offload pd_decode_mempool_shadow; do
  python3 -u ascend-mempool-test/scripts/verify_resources.py \
    --mode "$mode" --device-id 0 \
    --report "/tmp/ticket03-s3-${mode}.json" \
    2>&1 | tee "/tmp/ticket03-s3-${mode}.log"
done
SH
```

脚本固定小配置：2 层、2 个真实 request rows（另有 row 0 padding）、context 16、
page size 128、native token capacity 256、BF16、KV 维度 512+64、Index K 128、
sparse top-k/cache width 2048。旧路径的非空选中位置只有 prompt 0 与 decode 2，
其余 top-k 列填 -1，以满足已经验收的固定 lookup 宽度。

| mode | host KV / 映射 / host length | main-KV staging | HBM sparse cache | native K/V | Index K |
| --- | --- | --- | --- | --- | --- |
| `pd_prefill_mempool` | 0 | 0 | 0 | >0 | >0 |
| `pd_decode_mempool` | 0 | 0 | >0 | 0 | >0 |
| `local_offload` | >0 | 0 | >0 | 0 | >0 |
| `pd_decode_offload` | >0 | >0 | >0 | 0 | >0 |
| `pd_decode_mempool_shadow` | >0 | >0 | >0 | 0 | >0 |

通过判据：5 次进程均退出 0，输出 `RESOURCE_PASS`，各 JSON `success=true` 且满足表内
资源矩阵。正式模式隐藏 SHM 分配立即失败；正式旧入口明确拒绝；两种 D staging 模式
完成索引重写与 staging→host；普通模式完整校验 compact write→host prefix read→
sparse miss→refill→HBM hit；所有模式的 native Index K 页可写读，D 请求 row 可安全复用。

gate 使用真实 NPU 分配/拷贝和完整 Ascend PD adapter 构造方法。仅父 transport 的启动、
网络发送和状态 sink 用 recorder 替换，不创建连接、不发送实际 PD payload。
因此它不证明 Index K 跨机传输或正式服务 readiness/Graph 正确，这些仍在 S4–S6 验收。
S2 已验收的 BM fetch/Graph gate 不被本脚本替代。

遇到非零退出、`RESOURCE_FAIL`、断言失败或模式缺失即不通过。请回传
`/tmp/ticket03-s3-version.txt`、5 份 JSON 和对应 `.log`，再记录硬件验收结论。
