# Ascend mempool storage

02 的第一部分：提供存储布局、BM handle/view 和临时 KV 写入接口。
PD 控制协议、attention 双写、scheduler 和参数接入由后续三部分完成。
当前没有修改原有 sparse PD 数据路径，也没有创建服务级 mempool。

## 文件与接口

| 文件 | 接口 | 职责 |
| --- | --- | --- |
| `config.py` | `MempoolConfig.make_mla_layout()` | 以实际 local layer 数、`kv_lora_rank`、`qk_rope_head_dim` 构造 P/D 布局；固定 16 个 slots，容量默认各 16384。 |
| `layout.py` | `KVLayout` / `PoolLayout` | 计算逻辑 shape、row/element offset、贡献大小、共同 stride；校验 BF16 和 UniDexCopy 范围。 |
| `manager.py` | `MempoolKVManager.initialize_rank_pair()` | 将 TP rank `i` 映射到 P 的 `base_port+i` store，设置 P/D 的 BM rank 0/1，启动 BM 并 join pool。 |
| `manager.py` | `MempoolKVManager.create/join/view/close()` | 拥有一个双 rank BM handle，验证映射，提供 view，并在 drain 后销毁。 |
| `manager.py` | `MempoolKVView` | 提供一个 layer 的逻辑 tensor、元素地址和同步 setup 写入；持有 manager 引用。 |
| `offload.py` | `MempoolWriteInputs` / `MempoolKVOffload.write()` | 使用固定 device metadata，把 temporary KV 通过 UniDexCopy 直接写入本侧 BM。 |

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
4. `MempoolWriteInputs(rows, device)` 的 slot/position 初始为 -1、valid 为 false。
   调用方用 `copy_()` 等原地更新这些 tensor，保持 Graph 输入地址不变。
5. `MempoolKVOffload.write(values)` 接受连续的 BF16 `[rows, N, D]` temporary KV。
   每个 valid source row 对应一个不同的目的坐标；无效/越界行不写入。
   即使全部 invalid，也会提交 kernel，使 zero-valid capture 保留运行时写入路径。
6. 写入不调用 host tensor read 或 synchronize。跨 stream 的 producer/consumer
   依赖由调用方维护。请求容量检查与写入完成状态由后续控制/集成层负责；bounds mask
   不能用来把超容量请求当作成功。
7. 本 manager 管理 pool 的生命周期。request slot 的 acquire/release 与持久 ownership
   留在 PD 控制层；`view.write_rows()` 和 `offload.write()` 均不分配 slot。
8. pool 和 view 必须覆盖整个 Graph 生命周期。`close(drain)` 的 drain callback
   需排空所有相关 reads/writes，并确保 Graph 不会再次提交；失败时不销毁 pool。
   关闭后的 Python view 拒绝返回地址，但已捕获的 raw pointer 无法靠 Python 检查拦截。

## 检查与当前边界

CPU 行为测试放在仓库根目录 `ascend-mempool-test/tests/unit/`，见该目录 README。
`test_pair_startup.py` 使用 BM SDK boundary fake 检查 16 对端口、BM rank、错误参数与
失败清理。它不代表 16 对真实 BM 会话已在服务中启动；第④部分接入时仍需用户运行
NPU 测试。
独立测试通过自己的 package path 加载本目录模块，绕过 `sglang/__init__.py`，
无需安装 SGLang 或启动 server；01 的 `pool` import 保留兼容入口。

01 已有双机 BM + remote fetch Graph 的硬件验证。新增 offload 的 raw destination
写入和真实 server shadow 路径尚未由用户在 NPU 验证，不能从 CPU 结果推断通过。
