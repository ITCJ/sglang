# Ascend mempool storage

提供 ticket02 的存储布局、BM handle/view、临时 KV writer 和 backend runtime。
①–③、A1–A4接口优化及④的scheduler/配置/Graph接线已实现。
开启 `SGLANG_NPU_ENABLE_MEMPOOL=1` 时，BM/runtime在Graph前创建，service/control在
既有AscendKVManager建立后附加。shadow双写保留原sparse PD路径；真实服务尚待NPU验收。

## 文件与接口

| 文件 | 接口 | 职责 |
| --- | --- | --- |
| `config.py` | `MempoolConfig.make_mla_layout()` | 以实际 local layer 数、`kv_lora_rank`、`qk_rope_head_dim` 构造 P/D 布局；固定 16 个 slots，容量默认各 16384。 |
| `layout.py` | `KVLayout` / `PoolLayout` | 计算逻辑 shape、row/element offset、贡献大小、共同 stride；校验 BF16 和 UniDexCopy 范围。 |
| `manager.py` | `MempoolKVManager.initialize_rank_pair()` | 将 TP rank `i` 映射到 P 的 `base_port+i` store，设置 P/D 的 BM rank 0/1，启动 BM 并 join pool。 |
| `manager.py` | `MempoolKVManager.create/join/view/close()` | 拥有一个双 rank BM handle，验证映射，提供 view，并在 drain 后销毁。 |
| `manager.py` | `MempoolKVView` | 提供一个 layer 的逻辑 tensor、元素地址和同步 setup 写入；持有 manager 引用。 |
| `offload.py` | `MempoolKVOffload.write(values, *, slots, positions, valid)` | 通过显式 metadata 将 temporary KV 写入本侧 BM；不持有另一套输入缓存。 |
| `rows.py` | `derive_kv_rows()` | 从普通 forward 张量推导 request row、全序列 token position 和 valid；目前有意保留 sparse manager 行推导的副本。 |
| `runtime.py` | `MempoolRuntime` | 持有固定设备 binding 表、per-layer writer、forward/Graph 边界及本地写入计数/完成事件。 |
| `runtime.py` | `KVRowBinding` / `KVWriteReceipt` | 标识一次本地 row attachment，保留 detach 后的完成事实；不表示 PD slot ownership。 |
| `runtime.py` | `initialize_for_model_runner()` / `model_forward_scope()` | Graph前建立BM/runtime；逐次eager、warmup/capture和replay的host边界。 |

PD接入仅新增 `disaggregation/ascend/mempool_service.py` 和 `mempool_tick.py`：
service投影真实Req并延迟native清理，tick统一TP observations/preflight/commit/outbox。
协议仍由原control拥有；服务读取实际BM nonce确认ZMQ peer与BM peer一致。
`config.py` 集中校验启动组合；NIC基址为每对留出2个端口，store仍为 `base_port+i`。

每侧 KV payload 为 `L * B_slots * S * N * D * 2` 字节；MLA 的 `N=1`，
`D=kv_lora_rank+qk_rope_head_dim`。每 rank 加 64 字节 probe，再向 1 GiB 对齐。
`create2` 的 local contribution 分别使用 P/D 的大小，共同 maximum 使用较大值。
每 layer span 必须不超过 `UINT32_MAX`，每 row 不超过 32 KiB；整个 pool 可以超过 4 GiB。

## 生命周期与写入约定

1. 调用方先完成进程级 `mf.initialize()`，并负责在现有 TransferEngine 停止后统一
   `mf.uninitialize()`；manager 不关闭这个共享 MF 环境。
2. 每个 TP worker 进程只负责一对 P_i/D_i。`initialize_rank_pair()` 检查 `i` 在
   `[0, 16)`，P 用 BM rank 0 启动 `tcp://P_host:base_port+i` 的 store，D 用 BM rank 1
   连接同一 URL。它拒绝由本 manager 在进程内重复启动第二对 BM pool，初始化 BM、
   创建 handle、join 并检查两侧映射；失败时清理本次创建的 BM 状态。成功返回的
   manager 在 `close(drain)` 后释放它负责的 BM context。低级 `create()/join()`
   仍由调用方负责 BM 初始化/退出。
   MF 1.1 不提供可靠的 Python API 检查外部代码是否已初始化 BM，调用方须保证此前
   没有其他 BM context。这一步只确定 BM store 与两侧局部 rank；后续控制协议还须
   核对 P_i/D_i 的实际身份。
3. `view(rank, layer)` 可引用两侧 KV；运行时写入仅允许本侧 view。
   `write_rows()` 使用同步 BM SDK copy，只用于捕图外的 setup。
4. `MempoolRuntime` 管理固定设备 `row_slot` / `row_prompt_len` 表，初始为 -1。
   `binding = bind(row, slot=..., prompt_tokens=...)` 原地更新表；安装 event 由下次
   forward 等待。接入层按 approved request attempt 保存这个不可变 attachment 对象，
   对真实 `req.kv.req_pool_idx` 调用 `assert_bound(row, binding)`，拒绝旧 attachment。
5. `MempoolKVOffload.write(values, *, slots, positions, valid)` 接受连续 BF16
   `[rows, N, D]` temporary KV，以及同设备的 int64 slots/positions、bool valid 向量。
   runtime 在设备上构造这些参数；P eager 每个 chunk 可使用不同的 rows。
   每个 valid source row 对应一个不同的目的坐标；无效/越界行不写入。
   即使全部 invalid，也会提交 kernel，使 zero-valid capture 保留运行时写入路径。
6. 写入不调用 host tensor read 或 synchronize。跨 stream 的 producer/consumer
   依赖由调用方维护。请求容量检查与写入完成状态由后续控制/集成层负责；bounds mask
   不能用来把超容量请求当作成功。
7. 本 manager 管理 pool 的生命周期。request slot 的 acquire/release 与持久 ownership
   留在 PD 控制层；`view.write_rows()` 和 `offload.write()` 均不分配 slot。
   `detach_row(binding)` 在本地 completion 消费后清除 row 映射并返回不可变 receipt，
   不释放 slot。调用方还须保证无未来 row 提交以及 native transfer 安全；正常 P
   须等 handoff 成功，KV_READY 本身不够。P slot 保留到 D 的 DONE，D 则先 whole-D
   drain 再 detach。receipt 按 attempt 保存，不能再以旧 row 查询已复用请求的进度。
8. pool 和 view 必须覆盖整个 Graph 生命周期。`close(drain)` 的 drain callback
   需排空所有相关 reads/writes，并确保 Graph 不会再次提交；失败时不销毁 pool。
   关闭后的 Python view 拒绝返回地址，但已捕获的 raw pointer 无法靠 Python 检查拦截。

## 检查与当前边界

CPU 行为测试放在仓库根目录 `ascend-mempool-test/tests/unit/`，见
[测试与两机运行说明](../../../../../../ascend-mempool-test/README.md)。
`test_pair_startup.py` 使用 BM SDK boundary fake 检查 16 对端口、BM rank、错误参数与
失败清理。它不代表16对真实BM会话已在NPU服务中通过；④代码已接线，用户仍需运行
README中的shadow服务gate。
独立测试通过自己的 package path 加载本目录模块，绕过 `sglang/__init__.py`，
无需安装 SGLang 或启动 server；01 的 `pool` import 保留兼容入口。

01 的 remote fetch Graph、③旧版本的双机 runtime writer gate 已由用户反馈通过。
10月1日用户回传了 bind/detach/writer 接口调整后的双机日志，两端均为20条PASS和
ALL_CHECKS_PASSED；详情及版本证据边界见ticket02。
④新增service/tick及native释放边界回归后，Mac共108项CPU测试通过；不替代真实server验收。
