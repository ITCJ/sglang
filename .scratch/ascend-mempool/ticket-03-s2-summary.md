# Ticket03 S2：BM fetch 接入 selected KV

日期：2026-10-04。基线：`3d2f3c6168`（S1）。本轮只实现
[Ticket03 S2](issues/03-prefill-direct-offload.md)。代码先按用户要求保持未暂存，
供用户核对；用户完成核对后已授权提交本次 S2 代码、测试和说明。
S2 初版提交为 `9036be2b0f`；本文件同时记录首次 NPU gate 的参数修正和重测结果。

**S2 独立 NPU gate 已通过，用户于 2026-10-04 确认没有问题。** 首次 K=8 的参数
错误修正为 K=2048 后，D 的 30 个 case 全部通过，P/D 均输出 `ALL_CHECKS_PASSED`。
Ticket03 仍为 open，整票验收项保留到正式服务验证；S1 完整环境回归及 S3–S6 仍待完成。

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

首次 NPU gate 反馈后的修正仅涉及测试入口和说明：

| 类型 | 路径 | 修正作用 |
| --- | --- | --- |
| 修改 | `ascend-mempool-test/scripts/verify_fetch.py` | fetch gate 默认 top-k 宽度改为 2048；不符合真实 lookup 算子约束时，在双机连接及 BM 分配前报错。 |
| 修改 | `ascend-mempool-test/scripts/verify_graph.py` | 共享 parser 支持调用方指定默认 top-k；原 copy-only gate 仍默认 64，仍支持宽度 8。 |
| 新增 | `ascend-mempool-test/tests/unit/test_fetch_gate.py` | 验证 fetch 默认宽度、P/D 两侧非法参数提前拒绝及原 copy-only CLI 兼容性。 |
| 修改 | `ascend-mempool-test/tests/unit/test_materialize.py` | 增加 K=2048、小 context、有效 P/D 位置及其余列/行 padding 的真实 materialization CPU 回归。 |

同步更新本总结、Ticket03 进度/Comments 和独立测试 README。生产 fetch/runtime 与
kernel 不涉及本次参数修正；用户显式指定 K=2048 的重测已通过，详情见第 6 节。

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
  仍可能引用的地址。独立 NPU gate 已验证本次配置下的地址持有与 Graph replay。
- 正式 fetch 与 shadow READBACK 同时启用会明确报错；避免把 BM selected KV 与自身比较。
  该保护不改变现有 shadow 服务的 READBACK 行为。

本轮依赖已有 attention 对 hit/miss/refill/map 的等待，把新增 fetch 工作串入 forward
完成流。完整 TP 服务下的 drain、取消、peer ownership 与 native 回收仍须 S5/S6 核对。

## 5. Mac 已执行的检查

### S2 初版提交前

环境：Mac，Python 3.9、CPU PyTorch，虚拟环境 `/private/tmp/ascend-mempool-s1`。

```bash
PYTHONPATH=ascend-mempool-test/src /private/tmp/ascend-mempool-s1/bin/python -B -m unittest discover -s ascend-mempool-test/tests/unit -q
/private/tmp/ascend-mempool-s1/bin/mypy --config-file ascend-mempool-test/pyproject.toml python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py python/sglang/srt/hardware_backend/npu/mempool ascend-mempool-test/src ascend-mempool-test/scripts
```

结果：审查修复后的独立 CPU suite **165 项通过**；mypy **26 个源文件通过**；本次
12 个 Python 文件的 Ruff、isort、格式和 AST 检查通过。两项审查发现均先用测试复现
失败，再修复；相关 fetch/materialization/runtime/readback 共 38 项定向测试通过，
随后因 writer/fetch 行为发生修复再次运行完整独立套件。初版 `--describe` 使用 K=8，
只确认两侧各贡献 1 GiB DRAM，没有运行 lookup 算子，未发现固定 K=2048 的约束。
Markdown 本地链接、命令参数与 `git diff --check` 在交付前检查。

CPU suite 中故障注入用例会输出预期的 FAIL/fault 日志，最终 unittest 为 OK。
本机缺少 NumPy 的 PyTorch 初始化 warning 未影响这些张量测试。未运行真实 NPU kernel、
跨机 BM、NPU Graph、完整 SGLang 启动或模型精度测试；S1 完整环境回归仍待补跑。

### 2026-10-04 首次 NPU 失败与参数修正

用户在 P/D 各 device 0 运行本文初版命令：P/D 容量 8/16、2 层、Graph width 16、
3 个真实 rows、top-k 宽度 8、block_dim 24/48、2 次 replay。两端完成 MAPPED/STAGED 后，
D 在第一次 warmup 的 `materialize_selected_kv()` → `slot_map_lookup()` 抛出：

```text
RuntimeError: slot_map_lookup requires topk=2048, got 8
```

P 收到 `success=False, checks=0` 后报告 D 校验失败；本轮没有通过 fetch case。
初版交付版本为 `9036be2b0f`，用户日志未附机器实际 Git SHA，重跑时仍须一并记录。
原始日志/报告使用 `/tmp/ticket03-s2-{p,d}.{log,json}`。

根因是交付命令和 fetch gate 默认值没有遵守真实 NPU lookup 的固定宽度合同。
`sgl-kernel-npu/csrc/sparsity_driven_kv_offload/slot_map_lookup/op_host/slot_map_lookup.cpp`
定义 `kFixedTopk=2048`，同时要求 slot-map context 宽度为 8 的倍数。
初版 CPU lookup 替身接受任意 K，未覆盖该约束。

修正后 fetch gate 使用 K=2048，非空 case 每行只填 2 或 4 个有效位置，其余列用 -1。
P/D BM 容量仍为 8/16，fixture 的 slot-map 宽度为 32；不要求 2048 个有效 KV。
原 copy-only gate 不调用该 lookup，保留其可变 K 行为。

新增 CLI 回归先在初版代码上失败，修正后与 materialization 定向回归共 **10 项通过**。
新 materialization 用例在 CPU 边界明确检查固定 K 和 context 对齐，并验证小容量下
P/D 有效内容与 padding。mypy **26 个源文件通过**，4 个改动 Python 文件的 Ruff、
isort 和格式检查通过。执行命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/tests/unit /private/tmp/ascend-mempool-s1/bin/python -B -m unittest test_fetch_gate test_materialize -v
/private/tmp/ascend-mempool-s1/bin/mypy --config-file ascend-mempool-test/pyproject.toml python/sglang/srt/hardware_backend/npu/sparsity_driven_kv_offload/config.py python/sglang/srt/hardware_backend/npu/mempool ascend-mempool-test/src ascend-mempool-test/scripts
/private/tmp/ascend-mempool-s1/bin/python -B ascend-mempool-test/scripts/verify_fetch.py --describe --s-p 8 --s-d 16 --layers 2 --kv-dim 576 --topk 2048
```

修正后的 `--describe` 确认两侧仍各贡献 1 GiB DRAM；selected/cache 张量随 K 增大。
上述本地检查没有执行 NPU kernel；用户后续重测结果单独记录在第 6 节。

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
3 个真实 request rows，其余 row 0 padding；prompt length 为 4。lookup 的 top-k 张量
宽度固定为 **2048**，下表之外的列均填 -1；有效位置数量与这个宽度不同。
独立 `kv_pattern()` 生成可逐元素复核的 P/D 数据；D payload 第一个 feature 改为每个 block/cycle 唯一的
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
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report /tmp/ticket03-s2-k2048-p.json 2>&1 | tee /tmp/ticket03-s2-k2048-p.log
```

### D 端随后启动

```bash
set -o pipefail
python3 -u ascend-mempool-test/scripts/verify_fetch.py \
  --rank 1 --head-ip <P_IP> --device-id 0 \
  --nic-url tcp://<D_NIC_IP>:24770 \
  --store-port 18873 --control-port 18874 --pool-id 0 \
  --s-p 8 --s-d 16 --layers 2 --heads 1 --kv-dim 576 \
  --graph-rows 16 --active-rows 3 --topk 2048 \
  --block-dims 24 48 --replay-cycles 2 --warmup 3 --timeout 600 \
  --report /tmp/ticket03-s2-k2048-d.json 2>&1 | tee /tmp/ticket03-s2-k2048-d.log
```

### 通过与失败判据

- 两侧退出码为 0，日志末尾有 `ALL_CHECKS_PASSED`，JSON `status` 均为 `passed`。
- D 有 **30 条 `FETCH_PASS`**，覆盖 2 个 block_dim × 3 轮 × 5 个 case；D JSON 的
  `checks` 长度为 30，所有 `copied_per_layer` 与上表一致，输出包括 padding 逐元素一致。
- P 保留源 pool 直到收到 D `DRAINED`，双方正常完成 `P_RELEASED` / `D_CLOSED`；
  P report 的 `decode_result.success` 为 true，`checks` 为 30。
- 任何数值/计数不符、runtime fault、未恢复的 NPU/SDK 异常、超时、缺少 case 或
  `DRAIN_UNCONFIRMED` 都不算通过。无法确认 drain 时按独立测试 README 的既有流程处理，
  不把超时当成可复用 BM 的证据。

后续回归保留两侧环境检查输出、完整 `.log`、两个 `.json`，以及执行代码版本/工作区 diff。

### 2026-10-04 用户 NPU 重测与确认

用户回传了 22:03:02–22:03:07 附近的 P/D 控制台输出，并确认“我认为没有问题了”。
P 为 `npu1-31`，D 为 `npu1-32`，每侧使用 device 0；仓库路径为 `/home/cryang/sglang`。
使用上文双机命令的 K=2048 配置：P/D 容量 8/16、16 slots、2 层、BF16 `[1,576]`、
Graph width 16、3 个真实 rows、block_dim 24/48、3 次 warmup、2 次 replay。
P/D 的 store/control 端口仍为 18873/18874，各自 NIC 端口为 24770，pool ID 为 0。

| 已核对的项目 | 用户日志中的结果 |
| --- | --- |
| P/D 最终结果 | 两端均输出 `ALL_CHECKS_PASSED`，随后返回 shell，无 traceback 或 `DRAIN_UNCONFIRMED`。 |
| case 覆盖 | D 共 30 条 `FETCH_PASS`：2 个 block_dim × eager/replay1/replay2 × 5 个 case。 |
| `p_miss` / `d_miss` | 两层每层的 P/D copy 行数分别为 `[6,0]` / `[0,6]`。 |
| `mixed` | 两层每层均为 `[6,6]`，来自独立 P/D slot 的 KV 内容一致。 |
| `all_hit` / `zero_valid` | 两层每层均为 `[0,0]`；命中不读 BM，padding 不引入有效 copy。 |
| 逐元素结果 | 每个 case 比较 37,748,736 个元素，包含全部 padding 行/列；与独立 host reference 完全一致。 |
| Graph 与复用 | 每个 block_dim 的同一 Graph 连续 replay 两轮，覆盖 capture(A)→eager(B)→replay(A)、detach/rebind 和 P/D slot 更换。 |
| 本轮 D 写入 | `decode_epoch` 从 -1 到 -6，每轮当前 writer 的标记通过比较，未被 setup/旧请求数据掩盖。 |

`verify_graph.run()` 在 D 同步排空、发送 `DRAINED`、P 释放并回复 `P_RELEASED`、
D 释放并回复 `D_CLOSED`，以及报告写入完成后才打印最终通过标记。两端最终标记
证明本次独立 gate 的关闭流程走完；完整 TP 服务的生命周期仍由 S5/S6 验证。

P 日志在 `MAPPED` 前出现一次对端 GVA 转换失败。现有 manager 的
`_wait_for_mappings()` 会重试尚未就绪的映射，只有两侧地址及层范围检查通过后才返回。
本轮随后到达 `MAPPED`，并通过 peer probe 和全部真实读取；该启动日志属于已恢复的
映射等待，不能据此推断所有 SDK ERROR 都可忽略。

本次用户实际仍使用 `/tmp/ticket03-s2-p.log`、`/tmp/ticket03-s2-d.log`，报告参数为
`/tmp/ticket03-s2-p.json`、`/tmp/ticket03-s2-d.json`。这些路径与首次失败相同；首次
失败证据保留在会话及第 5 节，不假设远端旧文件仍存在。
交付基线为 `9036be2b0f`；机器实际 Git SHA、环境版本报告及 JSON 内容未随本次消息
提供，未将其记为已独立核查。本次通过结论依据用户完整控制台结果、脚本通过条件及确认。

S2 的实现核对和独立 NPU gate 均完成。当前服务仍使用 shadow；本次结果不覆盖
正式资源构造、PD 传输切换、NPU attention 算子或模型精度。Ticket03 保持 open。

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
复核未发现新的阻塞项；随后用户已确认上述独立 NPU gate 通过。

审查统计：Standards 为 0 项硬性违反、1 项非阻塞建议；Spec 为 2 项 P1 已修复、0 项遗留阻塞。

## 7. 后续步骤

S2 的实现及独立 NPU gate 已获用户确认。下一开发步骤为 S3；S1 待验证回归仍须补齐。
S3 按 mode 停用旧 host KV 和 main-KV staging 分配/写入，保留 HBM cache 与
Index K；S4 再切换实际 PD buffer 清单及传输契约；S5/S6 完成服务生命周期、可观察
证据、普通路径回归和用户 NPU 验收。只有整票验收通过后才关闭 Ticket03。
