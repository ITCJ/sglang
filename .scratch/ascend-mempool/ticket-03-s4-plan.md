# Ticket03 S4：复用 KVArgs，在 Ascend 发送入口选择传输路径

2026-10-05，基于 `5b18a8046c` 核对现有代码，按用户最新决定修订。
S3 NPU 资源 gate 已获用户确认；S4 已按本方案实现，用户于2026-10-05确认双机 NPU gate通过。
整票要求见 [Ticket03](issues/03-prefill-direct-offload.md)，交付流程见 [verification](verification.md)。

## 本版决定

继续使用公共 `KVArgs`，使用其已有 buffer 地址、长度、步长、层号和辅助状态字段。
模式信息由已有 Ascend mempool service/control 提供，发送端据此选择实际路径。

- 不新增 `AscendKVArgs` 或 `disaggregation/ascend/args.py`。
- 不修改 `get_kv_class()`；不向 KVArgs 动态附加 mempool 属性。
- 不单独新增 `mempool_transfer.py`；小范围布局检查放进现有 Ascend 文件。
- 不新增 Mooncake worker hook；原页索引、chunk、aux 和 drain 流程继续复用。

实际生产改动为 **5 个已有 Ascend/NPU 文件**，共享 `utils.py` 保持原样，
不新增生产文件。测试脚本和交付文档另计。

## 一、按模式选择发送内容的具体入口

源码中的 [AscendKVSender](../../python/sglang/srt/disaggregation/ascend/conn.py)
当前直接继承 `MooncakeKVSender`。后者的 `send()` 负责累计页数、判定末 chunk、
将请求入队；它不直接取得目标 buffer 列表。

实际构建复制任务的 Ascend 入口是 `AscendKVManager.send_kvcache()`。
在这里按 service 配置的模式分支，符合“进入发送端后判断模式和传哪些内容”的职责安排：

```text
AscendKVSender.send()
  -> 既有页数累计 / index_slice / is_last_chunk
  -> 既有 transfer queue 与 worker
  -> AscendKVManager.send_kvcache()
       普通或 shadow -> 原发送实现
       正式 mempool  -> 核对 Index K 条目，复用 generic copy
  -> 末 chunk 仍由原 worker 发送 state / aux
  -> 原 Success / Failed / abort drain
```

下面概括实际分支位置：

```python
# AscendKVManager.send_kvcache() 的逻辑示意。
if self._mempool_transfer_layout is not None:
    # 从原 KVArgs / 目标注册表读取 buffers、layer IDs、stride。
    # 检查失败返回错误码，让原 worker 完成失败与 drain 收尾。
    return self._send_mempool_index_k(...)

# 继续执行当前普通 / shadow 发送代码。
```

正式分支不再使用“前 2×layers 为 main K/V、剩余为 Index K”的 staging 假设。
它消费已经发布的 Index K entries，使用原 native 页索引和已有复制实现。
不把 `kv_indices` 置空，也不跳过整个 sender，否则会破坏末 chunk 与 aux 完成条件。

## 二、注册早于发送，buffer 列表仍要提前过滤

只在 `send_kvcache()` 中不复制 main K/V，可以关闭对应流量；
但无法撤销此前的内存注册，也无法让没有 main K/V buffer 的正式 D 发布旧列表。
S4 的要求包含 main-KV 发布/注册条目为零，所以保留 NPU pool 的发布分支：

```text
NPU pool 根据已确定的 mode 发布 buffers
  -> 原 KVArgs 保存地址 / 总字节数 / 页步长 / 层号
  -> AscendKVManager 初始化并注册这些 buffers
  -> 创建 mempool service/control，完成 peer 握手
  -> 真实请求准入
  -> Ascend 发送入口按模式构建 copy
```

在 `NPUMLATokenToKVPool.get_contiguous_buf_infos()` 中：

| 模式 | 发布给 PD transport 的内容 |
| --- | --- |
| 普通 / shadow | 沿用现有 main K/V 或 D staging，加实际 Index K |
| 正式 P / D | 仅实际 BF16 Index K；拒绝 FP8/scale 布局 |

正式分支在访问 main K/V 前返回，并完成本地 buffer 数量、长度与层号的一致性检查。
`get_kv_layer_ids()` 与 buffer 列表使用同一顺序。P native HBM K/V 继续用于 prefill，
只是不作为 PD payload 发布。D 保持 S3 已停用 main-KV staging 的资源策略。
正式模式继续受现有 BF16、固定拓扑等约束；S4 不扩展新的量化模型组合。

## 三、原 KVArgs 如何承载新列表

原类型及工厂均保持现状。两侧仍然先创建 `KVArgs()`，再填入实际发布的列表：

| 已有字段 | 正式模式中的内容 |
| --- | --- |
| `kv_data_ptrs` | Index K 条目的实际地址 |
| `kv_data_lens` | 对应 buffer 的总字节数 |
| `kv_item_lens` | 每项实际页步长 |
| `kv_layer_ids` | 每项实际 indexer 层号，允许不连续 |
| `kv_buf_groups` | 本次 BF16 布局固定为一个 Index K 组 |
| `page_size`、`kv_cache_dtype_str` | 沿用当前配置语义；具体条目 dtype 从实际 pool 核对 |
| `state_*`、`aux_*` | 沿用原来的必要状态与 handoff metadata |

例如真实 indexer 层号为 `[1, 4, 7]`，KVArgs 中就填入 `[1, 4, 7]`。
不能从条目数量推算成 `[0, 1, 2]`，也不能以 `entries // total_layers` 推断正式组数。

## 四、在现有 service 内补齐元数据

`disaggregation/utils.py` 保持原样。地址/长度由 NPU pool 提前过滤，原流程先用这些
字段注册内存。随后，`MempoolPDService.from_scheduler()` 同时取得实际 pool 与 manager，
调用 `AscendKVManager.configure_mempool_transfer(pool)`，填入原 `KVArgs.kv_layer_ids`
和 `kv_buf_groups=1`；地址、长度和页步长必须仍与已发布列表一致。

这一步发生在 scheduler 的 service 构造期间，早于请求接收及 D receiver 的
`_register_kv_args()`。物理注册只消费地址/长度，因此无需为层号/组数修改共享组装代码。
例如 `[1,4,7]` 三个 Index K 条目、模型共 8 层时，原组装临时得到的空层号/零组数，
会在对端可见之前补成 `[1,4,7]` / `1`。本次 BF16 范围没有 scale 组件。

## 五、sender 的 mode 从哪里取得

采用已有 service/control 接线，不借 KVArgs 传模式：

```text
NPU pool 上已确定的 sparse_kv_offload_mode
  -> MempoolPDService.from_scheduler()
       取得实际 pool 与 KVManager
       构造当前模式的传输信息
  -> 既有 PoolPeer / attach_mempool_control()
  -> AscendKVManager.send_kvcache() 读取本地 control 模式
```

`MempoolPDService.from_scheduler()` 已能访问实际 model runner、pool 和 manager。
在这里从 pool 的已解析 mode 得到传输种类：正式为 `index_k_only`，shadow 为完整原路径。
字段名为 `transfer_kind`，放在既有 `PoolPeer` 内；sender 不每个 chunk 重读环境变量；manager 保存该次配置的布局。
两侧 role 仍由原字段表示，P/D 的正式角色映射到同一种传输种类。

普通模式没有 mempool control，走原实现。shadow control 表明走原完整传输路径。
正式请求仍必须经过既有 handshake/binding 准入；control 尚未就绪时不发送真实请求。
独立 gate 显式构造同样的 control 状态；正式服务默认选择仍按 S5 的启动接线推进。

## 六、兼容检查放在现有 Ascend 文件

Index K 的 copy 和分页算法继续复用；检查聚焦于过滤 main K/V 后的新布局。
两端若一边发布 `[K, V, Index K]`，另一边发布 `[Index K]`，不能仅按位置配对。

| 位置 | 检查内容 |
| --- | --- |
| NPU 发布 / 参数组装 | 本地地址、长度、条目顺序和真实层号一致；正式列表不含 main K/V |
| mempool service 构造握手 | 从已有 KVArgs 和真实 pool 提取当前模式、page size、有序组件/层号/dtype/stride、必要 aux/state 布局 |
| `mempool_protocol.py` 既有 peer 校验 | 两端传输种类及布局兼容；绑定实际 transport session；旧版本明确失败 |
| Ascend 正式发送入口 | 用 session 读取继承的完整目标注册表，核对实际 entries、层号和逐项 stride 后调用 generic copy |

握手字段放在已有协议文件，与其编码、校验一起维护；不为此新增 args 或 transfer 模块。
实际地址和总容量做本地有效性检查，不要求 P/D 地址或总页数相同。
`POOL_HELLO/POOL_READY` 在真实请求准入前完成，正式与 shadow 的混用在这里拒绝。
协议 payload 版本升为 2，保留旧 router 能识别的 `ASCEND_MEMPOOL_V1` 路由 tag。

发送入口的预期校验失败返回错误码，复用原 worker 的 `conclude_failure` 和计数收尾。
空末 chunk 会绕过 `send_kvcache()`，因此 Ascend 的 `send_aux()`/`maybe_send_extra()`
也复用同一目标检查。原 worker 的索引裁剪、aux/drain 均保留，并通过回归验证。
不新增 `_validate_transfer_target()`，不新增通用页索引等长校验。

## 七、生产代码修改表

路径相对 `python/sglang/srt/`；所有文件均已存在。

| 路径 | 改动内容 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | 提供正式模式的 Index K-only 能力判断；按阶段保留服务启动保护 |
| `hardware_backend/npu/memory_pool_npu.py` | 正式模式发布 Index K；同步实际层号/组件组和本地元数据检查；保留 P native HBM 分配 |
| `disaggregation/ascend/conn.py` | 在现有 `send_kvcache()` 中按 control 模式分流，检查实际目标布局，复用 generic copy 和旧发送分支 |
| `disaggregation/ascend/mempool_service.py` | 从实际 pool mode、现有 KVArgs 和 transport session 构造握手信息，接入现有 control |
| `disaggregation/ascend/mempool_protocol.py` | 在既有 peer 消息中承载、编码和核对模式及布局；处理版本兼容 |

测试新增/修改位于 `ascend-mempool-test/`，不计入生产文件数量。

## 八、控制与数据生命周期

```text
P native HBM compact KV：服务 prefill 计算
P / D compact KV：经既有 writer 写 BM，D 经 S2 fetch 供 attention 使用
Index K：经原 PD transport 到 D native pages
state / aux / 首 token：经原末 chunk handoff

matching KV_READY + native Success + metadata + TP 一致且未取消 -> D 准入
P handoff + 本地 BM 写完 -> detach -> P native pages 回收
D 读完并 drain -> DONE -> P 确认安全与 detach -> P BM slot 释放
```

当前 BM 的物理存储为 DRAM。P native HBM cache 与 BM 是不同存储。
`_staging_outstanding` 仍承担 Index K/aux chunk 的 drain 计数，S4 保留其用途。

## 九、实施顺序与验收

1. 调整 NPU 正式 buffer 发布；现有 service 在 receiver 发布前填好原 KVArgs 的逻辑元数据。
2. 在 service/protocol 中接通真实 mode 和 peer 布局，保留旧路由和明确版本拒绝。
3. 在 Ascend `send_kvcache()` 加正式分支，继续使用原 sender、worker 和复制方法。
4. 交付真实双机 NPU gate 与普通/shadow 回归说明。

CPU 检查覆盖实际 buffer 发布结果、原 KVArgs 类型、非连续层号/页、模式分支、peer
不兼容、目标布局错误和失败收尾；复用 worker 验证 chunk、空末 chunk、aux 与 drain。
不以仅验证 enum 或 mock copy 调用次数替代真实数据检查。

双机脚本拟为 `scripts/verify_pd_transfer.py`。记录 main-KV 注册条目与发送字节为零，
用独立 oracle 校验 Index K、必要 aux 和未选页。覆盖两种 readiness 顺序、metadata
延迟、失败/取消及资源回收。正式服务选择与完整 TP16 模型验收仍由 S5–S6 推进。
S4 代码与脚本已交付，NPU gate 等待用户执行；见 [S4 总结](ticket-03-s4-summary.md) 和
[运行说明](../../ascend-mempool-test/PD_TRANSFER.md)。独立 gate 使用真实传输与控制类，
BM 完成事实由 fixture 提供，不声称覆盖完整 scheduler、BM 数据或 TP16 collective。
