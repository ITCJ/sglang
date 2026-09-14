# 传输路径命名与旧名对应

CSV 的 `path` 和 JSON 的 `path` 使用语义名称；`code` 和终端仍用短码，方便远端手工回报。旧结果不改写，按此表对应。

| 旧名 | 短码 | 语义名称 | 实现 |
| --- | --- | --- | --- |
| A | A | `L2-L1_SGLKernel` | 本地 L2 经 SGL kernel 到 L1 |
| B | B | `L3-L2-L1_Mooncake_Staging` | Mooncake → 额外暂存 → L2 → SGL kernel → L1 |
| C | C | `L3-L2-L1_Mooncake` | Mooncake 直接读入 L2 → SGL kernel → L1 |
| A′（新对照） | E | `L2-L1_MemFabric` | 本地 BM Host L2 → MemFabric → L1 |
| C′（新对照） | F | `L3-L2-L1_MemFabric` | 远端 BM Host L3 → 本地 BM Host L2 → L1，两段均 MemFabric |
| D | D | `L3-L1_MemFabric` | 远端 BM Host L3 → MemFabric → 最终 L1 |

SGL kernel 指 `sgl_kernel_npu.kvcacheio.transfer_kv_dim_exchange`，对应 HiCache Ascend MLA 的 `kernel_ascend / page_first_kv_split` 后端。

E/F/D 都在 `fabric_direct_bench`。它们共用 BM SDMA、同一源数据、相同 L1 地址、每批最多 8 页。E/F 的 L2 使用与 A/C 相同的分离 KV/RoPE page-first 布局、9 个槽（含保留页）；F 不增加接收暂存。E 在计时前填充 L2，F 将两段传输及中间等待计入时间。三路径逐批轮换顺序，每条路径测后分别校验。

A/E、C/F 用于同路径的软件实现对照；F/D 用于同软件栈的中转/直达对照。仍存在 ADXL 与 BM 内存分配器、Store 管理开销和源对象连续性差异，不将所有差异归因于单一算子。新 E/F 尚需远端一页校验，脚本自动先校验再扩大。
