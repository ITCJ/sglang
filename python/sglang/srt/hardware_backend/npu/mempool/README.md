# Ascend mempool storage

02 的第一部分：提供存储布局、BM handle/view 和临时 KV 写入接口。
PD 控制协议、attention 双写、scheduler 和参数接入由后续三部分完成。
当前没有修改原有 sparse PD 数据路径，也没有创建服务级 mempool。

## 文件与接口

| 文件 | 接口 | 职责 |
| --- | --- | --- |
| `config.py` | `MempoolConfig.make_mla_layout()` | 以实际 local layer 数、`kv_lora_rank`、`qk_rope_head_dim` 构造 P/D 布局；固定 16 个 slots，容量默认各 16384。 |
| `layout.py` | `KVLayout` / `PoolLayout` | 计算逻辑 shape、row/element offset、贡献大小、共同 stride；校验 BF16 和 UniDexCopy 范围。 |
| `manager.py` | `MempoolKVManager.create/join/view/close()` | 拥有一个双 rank BM handle，验证映射，提供 view，并在 drain 后销毁。 |
| `manager.py` | `MempoolKVView` | 提供一个 layer 的逻辑 tensor、元素地址和同步 setup 写入；持有 manager 引用。 |
| `offload.py` | `MempoolWriteInputs` / `MempoolKVOffload.write()` | 使用固定 device metadata，把 temporary KV 通过 UniDexCopy 直接写入本侧 BM。 |

每侧 KV payload 为 `L * B_slots * S * N * D * 2` 字节；MLA 的 `N=1`，
`D=kv_lora_rank+qk_rope_head_dim`。每 rank 加 64 字节 probe，再向 1 GiB 对齐。
`create2` 的 local contribution 分别使用 P/D 的大小，共同 maximum 使用较大值。
每 layer span 必须不超过 `UINT32_MAX`，每 row 不超过 32 KiB；整个 pool 可以超过 4 GiB。

## 生命周期与写入约定

1. 调用方先完成进程级 MF/BM 初始化。manager 不调用全局 initialize/uninitialize，
   后续需由服务协调 BM 与现有 TransferEngine 的共同生命周期。
2. 创建本进程对应的 pool rank：P 为 0，D 为 1。`join()` 检查两侧 GVA stride、
   每 layer 与贡献末尾的连续 device mapping；完成前 view 不返回可用地址。
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
独立测试通过自己的 package path 加载本目录模块，绕过 `sglang/__init__.py`，
无需安装 SGLang 或启动 server；01 的 `pool` import 保留兼容入口。

01 已有双机 BM + remote fetch Graph 的硬件验证。新增 offload 的 raw destination
写入和真实 server shadow 路径尚未由用户在 NPU 验证，不能从 CPU 结果推断通过。
