# 传输路径命名与旧名对应

终端、CSV 和 JSON 全部使用语义名称，不再输出路径短码或 `code` 字段。下面的短码列仅用于解释历史结果；旧结果不改写。

| 旧名 | 短码 | 语义名称 | 实现 |
| --- | --- | --- | --- |
| A | A | `L2-L1_SGLKernel` | 本地 L2 经 SGL kernel 到 L1 |
| B | B | `L3-L2-L1_Mooncake_Staging` | Mooncake → 额外暂存 → L2 → SGL kernel → L1 |
| C | C | `L3-L2-L1_Mooncake` | Mooncake 直接读入 L2 → SGL kernel → L1 |
| A′（新对照） | E | `L2-L1_MemFabric` | 本地 BM Host L2 → MemFabric → L1 |
| C′（新对照） | F | `L3-L2-L1_MemFabric` | 远端 BM Host L3 → 本地 BM Host L2 → L1，两段均 MemFabric |
| D | D | `L3-L1_MemFabric` | 远端 BM Host L3 → MemFabric → 最终 L1 |
| 新增 | — | `L3-L2_Mooncake` | Mooncake 直接读入最终 Host L2，只计远端读取 |
| 新增 | — | `L3-L2_MemFabric` | BM G2G 直接读入客户端最终 Host L2，只计远端读取与 BM wait |

两项单段实验复用中转路径的读取与地址布局，每轮清空 L2，单独采样。Mooncake 在计时外直接检查 Host 内容；MemFabric 在计时外用已有的本地 GH2L 读出 L2 并逐字节校验，此验证拷贝不计入单段耗时。保留原五档容量和两种布局；对单段而言连续/分散指后续 L1 映射，两者 L2 地址组织相同，不能解释成 L2 碎片对照。时间单位不变；统计改为整请求实际耗时，见套件 README。状态/错误标记不属于路径名。

SGL kernel 指 `sgl_kernel_npu.kvcacheio.transfer_kv_dim_exchange`，对应 HiCache Ascend MLA 的 `kernel_ascend / page_first_kv_split` 后端。

E/F/D 都在 `fabric_direct_bench`。它们共用 BM SDMA、同一源数据、相同 L1 地址、整请求提交。E/F 的 L2 使用与 A/C 相同的分离 KV/RoPE page-first 布局、请求页数加一个保留槽；F 不增加接收暂存。E 在计时前填充 L2，F 将两段传输及中间等待计入时间。每条路径以完整请求预热、采样，每轮在计时外校验。

A/E、C/F 用于同路径的软件实现对照；F/D 用于同软件栈的中转/直达对照。仍存在 ADXL 与 BM 内存分配器、Store 管理开销和源对象连续性差异，不将所有差异归因于单一算子。整请求版本尚需远端校验，脚本自动先校验再扩大。

Staging 仅保留为历史名称，不再进入性能套件。
