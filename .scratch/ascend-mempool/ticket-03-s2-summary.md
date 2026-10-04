# Ticket03 S2：BM fetch 接入 selected KV

日期：2026-10-04。基线：`3d2f3c6168`（S1）。本轮只实现
[Ticket03 S2](issues/03-prefill-direct-offload.md)。代码先按用户要求保持未暂存，
供用户核对；用户完成核对后已授权提交本次 S2 代码、测试和说明。
对应版本以本文件所属的 S2 Git commit 为准。

S2 代码及独立验证脚本已实现；Mac CPU 检查通过，等待用户执行 NPU 验证。
Ticket03 仍为 open，验收项未勾选。用户授权继续实施 S2 不等于 S1 或 S2 的硬件验收通过。

## 1. 交付范围

正式 D 分支现在把 P/D BM miss 的结果直接填入 attention 消费的 selected KV，保留
HBM sparse cache 的 lookup、hit、refill 和 slot-map 更新。正式 fetch 不依赖 READBACK，
也不通过诊断 scratch 间接提供数据。

现有 `SGLANG_NPU_ENABLE_MEMPOOL=1` 启动仍选择 Ticket02 shadow 模式。
`PD_DECODE_MEMPOOL` / `PD_PREFILL_MEMPOOL` 的正式服务启动保护保留，错误信息更新为
S3–S5 尚待完成。S3 的 host/staging 分配与 host 写入停用、S4 的 main compact-KV 传输
切换，以及 S5 的完整生命周期核对均未在本轮实施。普通模式和 shadow 模式保留原
host miss 分支。不能把本次独立 gate 通过称为正式服务 cutover 完成。

BM 当前由 DRAM 贡献并映射为 NPU 可访问地址；P native HBM KV cache、D HBM sparse
cache 与 BM 是不同资源，本轮没有改变其物理存储类型。

## 2. 修改与新增文件

下列生产路径相对 `python/sglang/srt/hardware_backend/npu/`。

| 类型 | 路径 | 本次作用 |
| --- | --- | --- |
| 修改 | `mempool/copy.py` | 新增 `KVFetch`，把 runtime binding、selected positions 和 miss mask 填入固定设备输入，复用 `SparseKVCopy` 两次 UniDexCopy；检查目标 tensor 身份，避免写入旧 selected buffer。 |
| 修改 | `mempool/runtime.py` | 增加正式 fetch 开关和接口；D 的 P slot 校验、实际已写长度维护不再依赖 READBACK；统一 selection 有效性，在设备上累计覆盖/越界证据，在完成事件后检查并进入 fault。 |
| 修改 | `sparsity_driven_kv_offload/manager.py` | `materialize_selected_kv(..., mempool_runtime=...)` 按 mode 校验依赖，仅切换 miss 来源；统一 valid mask 供 hit/miss/refill/map 使用并返回给 attention。类型注解和分配依赖延迟导入，使独立验证可加载真实 materialization。 |
| 修改 | `sparsity_driven_kv_offload/attention.py` | 将 padded `topk_2d` 传给 materialization，使用返回的有效性计算 attention indices/length；正式 D 将 backend runtime 传入，并跳过 shadow readback 比较。prefill CP 工具改为在 prefill 分支导入。 |
| 修改 | `sparsity_driven_kv_offload/config.py` | 保留正式模式启动保护，把尚待接通的步骤改为 S3–S5；不改变现有环境变量到 shadow 模式的映射。 |

下列路径相对仓库根目录。

| 类型 | 路径 | 本次作用 |
| --- | --- | --- |
| 新增 | `ascend-mempool-test/tests/unit/test_fetch.py` | READBACK 关闭的真实 runtime fetch：独立 P/D slots、当前 D 写入、padding、目标更换、capture 零有效 copy、非法读取、跨 forward 快照及覆盖检查。 |
| 新增 | `ascend-mempool-test/tests/unit/test_materialize.py` | 真实 manager/attention 的 BM miss→refill→hit、旧 host 分支回归、短 top-k、多请求、row 0、reset 复用与无 runtime 时拒绝回退。只替换不可用的 NPU kernel/stream 边界。 |
| 新增 | `ascend-mempool-test/src/ascend_sparse/fixture.py` | 仅供测试分配 HBM cache、slot map 和 stream/event，加载生产 materialization；不创建旧 host buffer，不验证正式服务构造器或 PD 启动。 |
| 新增 | `ascend-mempool-test/scripts/verify_fetch.py` | 双机 NPU gate：实际 BM writer、双源 fetch、HBM hit/refill、同一 Graph 重放、P/D slot 更换和逐元素校验。 |
| 修改 | `ascend-mempool-test/scripts/verify_writer.py` | writer gate 的 D binding 补充 P slot，满足 READBACK 关闭时也必须完整绑定的合同；该 gate 本身仍只验证 writer。 |
| 修改 | `ascend-mempool-test/tests/unit/test_runtime.py` | 更新原 D writer 测试的 binding 输入，保持原有 lifecycle/write 测试。 |
| 修改 | `ascend-mempool-test/tests/unit/test_sparse_config.py` | 校验正式启动保护的新阶段提示。 |

文档同步更新：本总结、Ticket03 的进度/Comments、生产 mempool README 和独立测试
README。没有修改 custom kernel、PD 协议、sender、staging allocator 或启动脚本。

## 3. 两次 kernel 如何工作

对 top-k 中一个全序列位置 `t`，从 request row 的稳定设备表读取 `p_slot`、`d_slot`、
`prompt_len` 和可读 `decode_len`。其中 `decode_len` 取调用方预期进度和本层实际连续
写入计数的较小值，包含本次 forward 当前层的有效写入，不能用生成 token 数推算。

```text
公共 valid = row 已绑定且非 padding
             且 0 <= t < max_context_len
             且 t < prompt_len + decode_len

hit  = valid 且 HBM slot map 命中
miss = valid 且 HBM slot map 未命中

P mask = miss 且 t < prompt_len
P source row = p_slot * P_capacity + t

D mask = miss 且 t >= prompt_len
D source row = d_slot * D_capacity + (t - prompt_len)

destination row = batch_row * padded_topk_width + topk_column
```

`SparseKVCopy` 还检查两侧 slot、容量和各自有效长度。P/D 使用不同 base pointer，
两次调用现有 UniDexCopy，在同一 miss stream 上依次执行，写入同一 selected tensor。
P/D mask 互斥且都是 miss 的子集，因此不会互相覆盖，也不会覆盖并行 hit stream 的输出。
即使 P 或 D 有效元素为零，仍提交这次 kernel，保证 dummy capture 不丢掉后续读取分支。

```text
binding / 本层 BM write / 公共 valid 与 lookup
                       |
                   copy_ready
                    /       \
          HBM hit copy       P BM copy -> D BM copy
             hit_done              miss_done
                    \       /
                     selected KV
                     /          \
                 attention      HBM refill

slot-map 更新从 copy_ready 后开始；返回 attention 前等待 refill_done 和 slot_map_done。
```

cache 的职责仍留在 `SparseKVCacheManager`；BM 地址、binding、writer/fetch 和完成事实
留在 `MempoolRuntime`。runtime 不接收 `Req`，不新增 ownership 状态机。

## 4. 有效性、Graph 与错误处理

- D `bind()` 必须提供已批准的 P slot，READBACK 为 0 时也校验。row 0 永远作为 padding。
- 正式 writer 同时核对设备位置是否等于 host 预期位置及本层已连续写入前缀的末尾。
  陈旧 `seq_lens` 造成的重复写或跳写不计入成功写入；后续排队 forward 也不能跨过空洞。
- 正式 `selected_kv_valid()` 在查 cache 之前检查可读范围，所有 copy 和 attention
  共用这份 mask；未绑定/padding 行排除，负索引作为无效 top-k。
- 真实已绑定行的非负越界位置记录为错误，即使旧 cache 恰好存在该位置的 hit，也不允许
  复制、回填或加入新的 slot map。传给 lookup kernel 的无效位置先替换为 -1，避免越界索引。
- coverage、invalid count、first invalid position 留在设备张量中；forward 末尾先 clone
  快照再 record completion event。`poll_completed()` 在 event 完成后读取快照并报错，
  进入 runtime fault，拒绝 detach 或后续 forward。后一次 forward 的清零不能覆盖旧错误。
- Graph 读取固定地址的 binding 表和输入张量；slot/长度/indices/mask 按次更新。
  `KVFetch` 缓存 copy 对象时同时检查 selected tensor 身份，防止同 shape 的新 eager
  或新 capture 目标沿用旧地址；更换目标时复用原输入 metadata，持续持有旧 Graph
  仍可能引用的地址。Graph 的实际固定地址及设备算子支持仍需本次 NPU gate 确认。
- 正式 fetch 与 shadow READBACK 同时启用会明确报错；避免把 BM selected KV 与自身比较。
  该保护不改变现有 shadow 服务的 READBACK 行为。

本轮依赖已有 attention 对 hit/miss/refill/map 的等待，把新增 fetch 工作串入 forward
完成流。完整 TP 服务下的 drain、取消、peer ownership 与 native 回收仍须 S5/S6 核对。

## 5. Mac 已执行的检查

环境：Mac，Python 3.9、CPU PyTorch，虚拟环境 `/private/tmp/ascend-mempool-s1`。

```bash
PYTHONPATH=ascend-mempool-test/src /private/tmp/ascend-mempool-s1/bin/python -B -m unittest discover -s ascend-mempool-test/tests/unit -q
/private/tmp/ascend-mempool-s1/bin/mypy --config-file ascend-mempool-test/pyproject.toml python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py python/sglang/srt/hardware_backend/npu/mempool ascend-mempool-test/src ascend-mempool-test/scripts
/private/tmp/ascend-mempool-s1/bin/python -B ascend-mempool-test/scripts/verify_fetch.py --describe --s-p 8 --s-d 16 --layers 2 --kv-dim 576 --topk 8
```

结果：审查修复后的独立 CPU suite **165 项通过**；mypy **26 个源文件通过**；本次
12 个 Python 文件的 Ruff、isort、格式和 AST 检查通过。两项审查发现均先用测试复现
失败，再修复；相关 fetch/materialization/runtime/readback 共 38 项定向测试通过，
随后因 writer/fetch 行为发生修复再次运行完整独立套件。`--describe` 确认下面的小配置
两侧各贡献 1 GiB DRAM。Markdown 本地链接、命令参数与 `git diff --check` 在交付前检查。

CPU suite 中故障注入用例会输出预期的 FAIL/fault 日志，最终 unittest 为 OK。
本机缺少 NumPy 的 PyTorch 初始化 warning 未影响这些张量测试。未运行真实 NPU kernel、
跨机 BM、NPU Graph、完整 SGLang 启动或模型精度测试；S1 完整环境回归仍待补跑。

## 6. 交付用户的双机 NPU gate

### 前置条件与输入

两台同一 superpod 的 Ascend 机器使用相同工作区版本、已有 CANN/torch_npu 环境、
`memfabric_hybrid==1.1.4`，以及提供 raw-pointer UniDexCopy 和 slot-map lookup 的
`sgl_kernel_npu`。每侧选择一张空闲 NPU；下面以本地 device 0 为例。
不启动模型/server/router。用实际地址替换 `<P_IP>`、`<P_NIC_IP>`、`<D_NIC_IP>`，
进入各自仓库根目录；store/control 端口 18873/18874 须可达且空闲。

两侧先分别执行：

```bash
export SGLANG_NPU_MEMPOOL_READBACK=0
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
python3 ascend-mempool-test/scripts/verify_fetch.py --check-env --device-id 0
```

独立 gate 直接创建 `fetch_enabled=True` 的 runtime，READBACK 为关闭状态，不通过环境
变量启动正式服务模式。测试 fixture 只分配 materialization 所需 cache/map/event，不绕过
或修改生产服务启动保护。

输入为 2 层、16 个 P/D slots、P 容量 8、D 容量 16、BF16 `[1,576]` KV、Graph width 16、
3 个真实 request rows，其余 row 0 padding；prompt length 为 4。独立 `kv_pattern()`
生成可逐元素复核的 P/D 数据；D payload 第一个 feature 改为每个 block/cycle 唯一的
负 epoch，独立 expected 同样核对。该标记区别于 setup 和旧请求数据，防止漏写/漏等时
碰巧读到预填的正确值。每次 forward 都真实写入新的 D KV，并依序测试：

| case | selected positions | 预期每层 P/D BM copy 行数（3 个请求） |
| --- | --- | --- |
| `p_miss` | 0、3 | 6 / 0 |
| `d_miss` | 4、5 | 0 / 6 |
| `mixed` | 0、3、4、6 | 6 / 6 |
| `all_hit` | 重复上一组，不 reset cache | 0 / 0 |
| `zero_valid` | 全部 -1 | 0 / 0 |

每个 block_dim 先用无绑定 dummy rows 捕图（P/D 均 zero-valid），再跑一轮 eager 和
两轮同一 Graph replay；capture 的目标为 A，eager 特意换成同形状 B，replay 仍检查
原目标 A，覆盖 metadata 存活。每轮 detach/rebind 到不同 P/D slot。24/48 core 分别执行。
测试 copy 计数只用于证明来源与命中；数据正确性仍由独立 host pattern 逐元素比较证明。

### P 端先启动

```bash
set -o pipefail
python3 -u ascend-mempool-test/scripts/verify_fetch.py \
  --rank 0 --head-ip <P_IP> --device-id 0 \
  --nic-url tcp://<P_NIC_IP>:24770 \
  --store-port 18873 --control-port 18874 --pool-id 0 \
  --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 8 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report /tmp/ticket03-s2-p.json 2>&1 | tee /tmp/ticket03-s2-p.log
```

### D 端随后启动

```bash
set -o pipefail
python3 -u ascend-mempool-test/scripts/verify_fetch.py \
  --rank 1 --head-ip <P_IP> --device-id 0 \
  --nic-url tcp://<D_NIC_IP>:24770 \
  --store-port 18873 --control-port 18874 --pool-id 0 \
  --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 8 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report /tmp/ticket03-s2-d.json 2>&1 | tee /tmp/ticket03-s2-d.log
```

### 通过与失败判据

- 两侧退出码为 0，日志末尾有 `ALL_CHECKS_PASSED`，JSON `status` 均为 `passed`。
- D 有 **30 条 `FETCH_PASS`**，覆盖 2 个 block_dim × 3 轮 × 5 个 case；D JSON 的
  `checks` 长度为 30，所有 `copied_per_layer` 与上表一致，输出包括 padding 逐元素一致。
- P 保留源 pool 直到收到 D `DRAINED`，双方正常完成 `P_RELEASED` / `D_CLOSED`；
  P report 的 `decode_result.success` 为 true，`checks` 为 30。
- 任何数值/计数不符、runtime fault、NPU/SDK 异常、超时、缺少 case 或
  `DRAIN_UNCONFIRMED` 都不算通过。无法确认 drain 时按独立测试 README 的既有流程处理，
  不把超时当成可复用 BM 的证据。

请回传两侧环境检查输出、完整 `.log`、两个 `.json`，以及执行代码版本/本次工作区 diff。
本说明交付时尚未取得这些硬件结果。

## Standards

硬性规范违反 **0 项**。非阻塞建议 **1 项 possible Data Clumps**：`KVFetch.gather()`
一起传入四张 binding 表，可考虑使用具名设备视图。目前只有一个生产调用点，且复用
既有 tensor 表，不为本轮额外引入抽象；作为接口整理建议保留。

## Spec

初审发现 **2 项 P1，均已修复并由同一规格审查代理复核**：

1. 同形状 output 更换时可能释放旧 Graph 引用的 metadata。现复用原 `SparseCopyInputs`，
   CPU 验证 weakref 存活与身份，NPU gate 增加 capture(A)→eager(B)→replay(A)。
2. host 计划的 decode 长度可能大于实际连续写入范围。现 writer 检查实际位置与预期/
   已写前缀，selection 取实际可读上界；陈旧 `seq_lens` 与后续排队跳写测试验证拒绝读空洞。

gate 同时增加 D epoch 标记，避免初始 staging 数据掩盖当前 writer 漏写或漏等。
复核未发现新的阻塞项；NPU 结果待用户反馈。

审查统计：Standards 为 0 项硬性违反、1 项非阻塞建议；Spec 为 2 项 P1 已修复、0 项遗留阻塞。

## 7. 后续步骤

本次 S2 代码已由用户核对。下一步运行上述 gate，再根据结果修正；同时补齐 S1 待验证回归。
后续 S3 按 mode 停用旧 host KV 和 main-KV staging 分配/写入，保留 HBM cache 与
Index K；S4 再切换实际 PD buffer 清单及传输契约；S5/S6 完成服务生命周期、可观察
证据、普通路径回归和用户 NPU 验收。只有整票验收通过后才关闭 Ticket03。
