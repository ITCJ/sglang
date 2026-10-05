# Ticket03 S4：Index K-only PD 传输交付

日期：2026-10-05。基线：`5b18a8046c085fda24e115bd2251ecf19b0898b7`。
S3 NPU gate 已获用户确认；S4 组件代码与双机 gate 已实现，等待用户执行 NPU 验收。
Ticket03 保持 `State: open`，正式服务选择仍为 shadow，S5 启动保护继续生效。

## 修改代码路径与功能

路径相对 `python/sglang/srt/`。生产改动只涉及以下五个已有文件。

| 文件 | 修改内容与作用 |
| --- | --- |
| `hardware_backend/npu/sparsity_driven_kv_offload/config.py` | 新增 `uses_index_k_only_transfer` 能力判断，仅正式 P/D 返回 true；启动保护提示推进到 S5。 |
| `hardware_backend/npu/memory_pool_npu.py` | 正式 `get_contiguous_buf_infos()` 在访问 main K/V 前返回实际 BF16 Index K buffers，验证地址/长度/stride；`get_kv_layer_ids()` 返回实际 indexer 层号。P native HBM 分配保留，D 无 main K/V 也可发布。 |
| `disaggregation/ascend/conn.py` | `configure_mempool_transfer()` 核对注册列表并补齐原 KVArgs 的层号/组数；正式 `send_kvcache()` 对照握手和原生注册表验证目标，调用原 generic copy；state/aux 入口复用检查，覆盖空末 chunk；错误返回码交给原 worker 收尾。 |
| `disaggregation/ascend/mempool_service.py` | 从实际 pool 调用配置入口，再构造带传输种类、布局和真实 transport session 的 PoolPeer；发生在 receiver 发布 KVArgs 和请求准入前。 |
| `disaggregation/ascend/mempool_protocol.py` | 新增该协议内部的 `IndexKTransferLayout`；PoolPeer 携带模式/layout/session；拒绝不兼容 peer；payload v2，保留原 `ASCEND_MEMPOOL_V1` 路由 tag 以便旧端明确拒绝版本。 |

继续使用公共 `KVArgs`。不新增参数子类、不向 KVArgs 附加 mempool 属性，不新增生产模块；
工厂、共享 `disaggregation/utils.py`、scheduler、Mooncake worker 均未修改。

## 运行链路

```text
已解析的 pool mode
  -> NPU pool 发布 Index K 地址/长度/页步长（main K/V 不发布）
  -> 原 KVArgs -> 原 manager 注册内存
  -> service 调用 configure_mempool_transfer
       核对注册列表；填实际层号，如 [1,4,7]；kv_buf_groups=1
  -> PoolPeer 握手：模式、native session、page/layer/dtype/stride/aux/state 布局一致
  -> receiver 发布原生注册信息 -> 请求绑定/准入
  -> 原 AscendKVSender 累计页数、切 chunk、入队
  -> 原 worker -> AscendKVManager.send_kvcache
       正式：验证实际目标注册 -> 原 generic copy，仅 Index K
       普通/shadow：原完整发送路径
  -> 末 chunk：原 state/aux -> 原 Success/Failed、outstanding 与 abort drain
```

地址过滤必须在注册前完成；逻辑层号和组数在 service 构造时补齐即可。
物理注册只读取地址/长度，D `_register_kv_args()` 要等到请求初始化才调用，因此无需
修改共享 utils。当前只支持 BF16、一个 Index K 组件组，FP8/scale、draft、staging
及不兼容 PP/DCP 布局明确拒绝；没有扩展新模型组合。

`kv_indices` 保留原 native page 含义。S4 不引入 BM slot 偏移、不清空整个发送请求。
空末 chunk 会跳过 `send_kvcache()`，所以 `send_aux()` 和 `maybe_send_extra()` 也核对
同一个 peer/session 与目标布局，避免绕过正式检查。

## 控制与释放

- D 仍需原 native Success、metadata 到达、KV_READY 和已有 TP 一致条件。
- P native HBM K/V 服务 prefill 计算；compact KV 的后续读取来源是独立的 BM。
- P native 页只有在原 handoff 与本地 BM 写入均完成后才能回收；KV_READY 本身不够。
- P BM slot 保持到 D 完成读取/drain、发 DONE 后才释放并回复 ACK。
- Index K 与 aux 仍有真实传输，因此保留原 outstanding、失败状态和 abort drain。

上述控制/释放算法沿用既有实现。本次联动测试使用实际 worker/control 方法验证关键顺序；
完整 scheduler 的 native row 释放与复用，以及真实模型 Graph 的证明仍由 S5/S6 完成。

## 新增与更新的验证文件

| 文件 | 作用 |
| --- | --- |
| `ascend-mempool-test/tests/unit/test_pd_transfer.py`（新增） | CPU tensor 与 memcpy 替代设备边界；运行生产发布/copy/sender/worker/control 方法，覆盖非连续页、多 chunk、空末 chunk、错误布局/session、aux 失败、取消、readiness、metadata 与普通/shadow。 |
| `ascend-mempool-test/tests/unit/test_pd_protocol.py` | 正式协议 round-trip，模式/层号/stride/page/aux 不兼容、旧版本拒绝。 |
| `ascend-mempool-test/tests/unit/test_sparse_config.py` | 正式服务仍受 S5 保护。 |
| `ascend-mempool-test/src/ascend_mempool/pd_transfer.py`（新增） | CPU 和 NPU gate 共用的测试构造器；只提供构造/网络边界，不复制发送或状态算法。 |
| `ascend-mempool-test/scripts/verify_pd_transfer.py`（新增） | 单对 rank 双机 NPU gate：真实 Index K/MetadataBuffers/TransferEngine、六个 case、范围与数据 oracle、SHA/版本核对。 |
| `ascend-mempool-test/scripts/verify_resources.py` | S3 资源 gate 跟进 S4：正式 pool 可发布 Index K，模型启动仍拒绝；其他资源回归保留。 |
| `ascend-mempool-test/PD_TRANSFER.md`（新增） | 前置条件、逐机命令、通过/失败标准、日志回传与验收边界。 |

## 验证记录

Mac 实际结果：独立测试包全量 CPU suite **190 tests 通过**；严格 mypy **31 文件通过**；
按独立测试配置进行的 Ruff 检查、**52 文件格式检查**、isort 和 `git diff --check` 通过。
测试没有启动 NPU 或跨机网络传输。协议升级要求 P/D 同版重启，v1/v2 混用会明确失败。

NPU：尚未执行。按 [双机 gate](../../ascend-mempool-test/PD_TRANSFER.md) 先启动 P、再启动 D。
独立 gate 的 BM 就绪事实由 fixture 提供，不证明真实 BM 数据、完整 TP16 collective 或
模型输出精度。用户验收通过后再进入 S5 的正式模式启动、Graph/stream 和生命周期接线。

## 阶段评审

### Standards

独立规范评审未发现硬性违反或确定的新增正确性缺陷。一个 P3 可选建议是复用 pool
和 manager 内相似的地址/长度/stride 检查。本次保留两处局部校验：前者保护 Index K
发布，后者保护 aux/state 注册，各自靠近资源拥有者，重复仅一个短谓词；不为此扩大模块接口。

### Spec

独立需求评审发现 NPU gate 的八层 BM 描述曾按一层计算容量，会在首次握手前失败。
已改为共用 `make_gate_descriptor()` 按完整维度计算，P 为 37,748,736 B、D 为
4,718,592 B、stride 为 37,748,736 B，并新增 CPU 测试调用真实 `PoolDescriptor`
验证这组 gate 参数。评审者复核通过，P1 已解决，无剩余阻塞项。

当前双侧正式配置强制 DCP=1，原 receiver 发布的参数使原 worker 不进入 DCP relayout
分支；无需新增共享 worker hook。硬件结果仍须按上述 gate 由用户确认。
