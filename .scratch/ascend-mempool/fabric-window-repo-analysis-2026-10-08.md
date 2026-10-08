---
title: A3 Fabric 窗口：两个库能解决什么？
subtitle: 对照本地源码、提交说明、官方 Wiki 与 npu1-31 地址快照
template: doc
theme: blueprint
lang: zh
date: 2026-10-08
---

## A 找到了官方修复方案

```callout ok 核心结论
**有官方方案可以解决这类地址重叠不足。**
MemCache 文档给出 BIOS 补丁和 `MSD Base Address Adjust` 设置。
方案调整服务器 DRAM 的物理地址布局，使它落入现有 HCCS 窗口。
2TB A3 的官方案例，从约 680GB 交集提升至接近整机容量。
**只升级 MemFabric 或 MemCache，不能据此认为你的机器已修复。**
```

本次检查的目录是 `/Users/hibikid/Documents/sparsekv-ascend-sglang/gitcode`。

| 仓库 | 本地 HEAD | 与本问题的关系 |
|---|---|---|
| `memfabric_hybrid` | `29fbd1ae14e7` · 2026-10-08 | 提供分配、地址映射和数据搬运 |
| `memcache` | `1909b0e8fe55` · 2026-10-07 | 调用 MF 建池；文档给出 BIOS 扩容方案 |

结论来自源码和官方材料；本次未在 NPU 上实施升级或分配测试。
你提到的博客尚未提供链接，以下不推定它与官方方案相同。

来源：[官方 A3 扩容说明](https://gitcode.com/Ascend/memcache/wiki/MemCache%E6%89%A9%E5%A4%A7A3%E5%8F%AF%E7%94%A8%E5%86%85%E5%AD%98%E7%A9%BA%E9%97%B4.md)、[MemCache 配套要求](https://gitcode.com/Ascend/memcache/blob/1909b0e8fe557868efbe6348ff8af8321a17194f/docs/zh/compatibility.md)。

## B 修复改变了哪一层？

```flow LR
适配的BIOS补丁 -> 调整DRAM物理布局
调整DRAM物理布局 -> 增加窗口交集
固定HCCS窗口 -> 增加窗口交集
增加窗口交集 -> 驱动可选择更多物理页
驱动可选择更多物理页 -> MF共享池扩容
```

原先的判断“奇数 NUMA 与窗口无交集”仍然成立。
新发现是：**厂商已经提供改变 DRAM 地址布局的方案。**
它使更多实际内存进入窗口，窗口本身可以保持不变。

| 官方文档列出的机型 | 文档列出的补丁入口 |
|---|---|
| Atlas 900 A3 SuperPoD | 1.0.8.1 配套中的 `Atlas-900-A3-SuperPoD-BIOS_32.85.zip` |
| Atlas 800T A3 | 1.0.5.1 配套中的 `Atlas-800T-A3-Atlas-800I-A3-BIOS_32.85.zip` |

官方步骤包含：通过 iBMC 升级、重启，并启用 `MSD Base Address Adjust`。
具体包和升级顺序应按服务器型号及配套指导书确认。
你的 `npu-smi` 和 HDK 26.1.1 信息，尚不足以确定应选哪个 BIOS 包。

**这也回答了之前的问题：平台支持时，NUMA 所属 DRAM 的物理地址布局可以调整。**
直接改驱动头文件中的地址常量，不能替代这项平台配置。

来源：[官方扩容说明：实施步骤与验证案例](https://gitcode.com/Ascend/memcache/wiki/MemCache%E6%89%A9%E5%A4%A7A3%E5%8F%AF%E7%94%A8%E5%86%85%E5%AD%98%E7%A9%BA%E9%97%B4.md)。

## C 用地址和数字对照你的机器

下表统一使用左闭右开区间，容量使用 GiB。
四个窗口各长 **682GiB**，总窗口长度是 **2728GiB**。
窗口编号与 Linux NUMA 编号是两套编号。

| 窗口 | HCCS 本机物理窗口 `[start, end)` | npu1-31 当前交集 |
|---|---|---|
| W0 | `0x29580000000` → `0x34000000000` | node0：170GiB；node1：0 |
| W1 | `0xa9580000000` → `0xb4000000000` | node2：170GiB；node3：0 |
| W2 | `0x129580000000` → `0x134000000000` | node4：170GiB；node5：0 |
| W3 | `0x1a9580000000` → `0x1b4000000000` | node6：170GiB；node7：0 |

以 node0 为例：它从 `0x28000000000` 开始，覆盖 256GiB。
W0 的起点比它高 86GiB，因此交集只有 `256 − 86 = 170GiB`。
node1 的两段地址都低于 W0 起点，也不进入其他窗口。

```limits
你的机器：窗口交集 | 680 / 2047 | GiB | 分母为此前在线内存块覆盖量
官方补丁后案例：窗口交集 | 2046 / 2048 | GiB | 来自另一台 2TB 机器的地址图
```

官方补丁后的 `lsmem` 截图，可换算为以下区间。

| 官方案例中的 DRAM 区间 | 块覆盖量 | 与原窗口交集 |
|---|---:|---:|
| `0x0` → `0x80000000` | 2GiB | 0 |
| `0x2c080000000` → `0x34000000000` | 510GiB | 510GiB |
| `0xac000000000` → `0xb4000000000` | 512GiB | 512GiB |
| `0x12c000000000` → `0x134000000000` | 512GiB | 512GiB |
| `0x1ac000000000` → `0x1b4000000000` | 512GiB | 512GiB |

**2046GiB 是按截图重算的地址交集，不是承诺能分配的空闲内存。**
低地址 2GiB 仍在窗口外；模型、系统占用和碎片仍会减少实际容量。
该截图没有 NUMA 标签，不能据此预报你升级后的节点编号和容量。

来源：[官方补丁后地址截图](https://raw.gitcode.com/user-images/assets/7672915/76025082-d093-4862-9496-703209ac9a79/4.png)、[MF 当前扫描脚本中的相同窗口](https://gitcode.com/Ascend/memfabric_hybrid/blob/29fbd1ae14e7862c2335dc643bdf8084f0253393/src/smem/python/memfabric_hybrid/memfabric_hybrid/mem_scan.py)。

## D “384 内要覆盖 24 台机器”如何理解？

这里的总线名称是 **HCCS**。
固定地址预算需要在机器和地址分段之间划分，这是合理的设计解释。
但本次读到的注释、提交和 Wiki，没有直接确认“24 台决定 682GiB”的推导。

| 判断 | 证据强度 |
|---|---|
| 每机有四个固定 DRAM 窗口，每个 682GiB | 已由安装版代码、MF 源码及官方 Wiki 支持 |
| 当前 2TB 机器仅约 680GiB 可进入这些窗口 | 你的地址快照与官方案例相符 |
| 窗口大小来自 24 台共享某个总线地址预算 | 数值上可解释；设计动机尚未证实 |

一个**条件推算**是：

```text
若 DRAM 总预算为 64TiB，且分给 24 台、每台 4 段：
64 × 1024 ÷ 24 ÷ 4 = 682.666… GiB / 段

代码实际值：682GiB / 段
4 × 682 × 24 = 63.9375TiB
```

这个数值吻合不能证明假设中的 64TiB 预算或机器编号规则。
此前地址公式的 `0x800000000000` 是全局区间起点，也不能当成容量。

**即使采用这套预算，每机 2728GiB 的窗口也大于你的约 2TiB DRAM。**
你只能得到 680GiB，直接原因是 DRAM 所在位置与窗口错开。
因此，保留总线预算并移动 DRAM 布局，仍可以恢复大部分容量。

## E MemFabric 新代码实际改进了什么？

当前 A3 的普通 BM / SDMA 路径仍使用 `MEM_P2P_DDR_TYPE`。
1GiB 页失败后可尝试 2MiB 页；物理页仍受驱动窗口限制。
当前 SGLang 的 P/D mempool 正是通过 `bm.create2(..., SDMA)` 建池。

| 功能或提交 | 作用 | 对本次问题的结论 |
|---|---|---|
| `2c6a2b5f` · 07-30 | 增加 `UNRESTRICTED_MEM`，切换为普通 `MEM_DDR_TYPE` | 本机 offload 有绕开 P2P 分配限制的路径 |
| `78fba957` · 09-21 | 从 SHARED offload 移除上述标志 | 提交明确说明它不支持跨机 import |
| `e973ccbb` · 07-18 | 增加 best-effort 分配 | 可接受较小实得容量；不会增加窗口交集 |
| `enable56BitsGva` | 扩大逻辑地址表达范围，按实际贡献映射 | 解决 GVA 预留空间问题；不会移动 DRAM |
| `DRAM_MAP_HOST_VA` | 选择 Host VA 映射方式 | 不等于解除 P2P 物理地址限制 |

当前 LOCAL offload 默认使用 `UNRESTRICTED_MEM`，选择 giant-page 模式时另走分支。
当前 SHARED offload 没有该标志。
它们不同的分配条件，不能直接套用到同一套跨机 BM 池。

best-effort 按 32GiB 切片尝试，失败后保留此前成功的容量。
如果在 SGLang 使用它，必须核对实际池大小，并据此校验 slot 布局。
沿用预先计算的请求容量，可能使布局超过实得空间。

来源：[分配路径](https://gitcode.com/Ascend/memfabric_hybrid/blob/29fbd1ae14e7862c2335dc643bdf8084f0253393/src/hybm/csrc/mm/hybm_vmm_based_segment.cpp)、[LOCAL offload](https://gitcode.com/Ascend/memfabric_hybrid/blob/29fbd1ae14e7862c2335dc643bdf8084f0253393/src/acc_offload/csrc/acc_offload_local_dram_entry.cpp)、[跨机 import 限制的提交说明](https://gitcode.com/Ascend/memfabric_hybrid/commit/78fba957b17563b832b40f0ee1c21e3697d8cc07)、[best-effort 实现](https://gitcode.com/Ascend/memfabric_hybrid/blob/29fbd1ae14e7862c2335dc643bdf8084f0253393/src/smem/csrc/smem_bm/smem_bm_entry.cpp)。

## F MemCache 能否替你解决？

```flow LR
MemCache配置 -> MmcBmProxy
MmcBmProxy -> MF的create2
MF的create2 -> HAL及驱动
HAL及驱动 -> HCCS可达物理页
```

`MmcBmProxy::InternalCreateBm()` 仍调用 MF 的 `SmemBmCreate2()`。
它传入容量、传输类型和 flags，并启用 56-bit GVA。
**在 A3 `device_sdma` 配置下，MemCache 继承相同的物理窗口约束。**

`315782b2` 将 56-bit GVA 改为默认开启，删除了 32TB 阈值判断。
它调整逻辑地址管理，不能让窗口外 DRAM 自动变成 HCCS 可达内存。
MF 的示例也明确区分了 GVA 表达范围与实际映射容量。

MemCache 的新增价值，是可直接找到这项 BIOS 修复的正式配套说明。
该说明的提交为 `0be7619e`，日期为 2026-09-18。
MF FAQ 后面的容量排查章节，也链接了同一份 BIOS 指导。

源码另有 RDMA 等路径，但那需要重新核对传输和分配方式。
当前 SGLang 算子直接读取远端 P 池地址，不能只改协议名就完成迁移。

来源：[MemCache 建池调用](https://gitcode.com/Ascend/memcache/blob/1909b0e8fe557868efbe6348ff8af8321a17194f/src/memcache/csrc/local_service/mmc_bm_proxy.cpp)、[56-bit GVA 提交](https://gitcode.com/Ascend/memcache/commit/315782b27623daaef5465d8f95ddbbfe6b98ee75)、[MF 56-bit GVA 示例说明](https://gitcode.com/Ascend/memfabric_hybrid/blob/29fbd1ae14e7862c2335dc643bdf8084f0253393/examples/memory_pool/04_features/03_enable_56bits_gva/README.md)、[MF FAQ](https://gitcode.com/Ascend/memfabric_hybrid/wiki/FAQ.md)。

## G 对当前 P/D mempool 的选择

| 方向 | 能解决什么 | 当前判断 |
|---|---|---|
| 按机型应用 BIOS 地址调整方案 | 增加 P、D 机器上的 HCCS 可达 DRAM | **优先核对，最贴合现有跨机读取设计** |
| 只升级 MF / MC | 获得新的功能与软件修复 | 不足以证明奇数节点可用于共享池 |
| D 池改用 LOCAL offload | 利用本机普通 DRAM 分配路径 | 值得单独评估；需要改接入并验证落点与 Graph |
| P 池改用 unrestricted 分配 | 可能扩大本地容量 | 跨机 import 不受支持，不能直接替换 |
| 改为 RDMA 搬运到本地再读取 | 使用不同传输路径 | 属于数据路径改造，需要独立设计和验证 |

BIOS 方案针对地址布局问题。
偶数节点的大容量分配长尾、碎片和失败后的析构崩溃，仍需分别复验。

## H 下一步只需先核对机型和 BIOS

在宿主机读取这些信息：

```bash
sudo dmidecode -t system
sudo dmidecode -t bios
lsmem
```

然后用已有脚本保存地址交集基线：

```bash
cd /home/cryang/sglang
python3 ascend-mempool-test/scripts/probe_numa_fabric_overlap.py \
  --driver-header /usr/local/Ascend/driver/kernel/svmdrv/pmaster/common/inc/devmm_common.h \
  --report /tmp/numa-fabric-before.json
```

1. 核对机型是否属于官方方案适用范围。
2. 向设备维护方核对对应 BIOS 和地址调整选项。
3. 完成平台调整后，重跑地址交集脚本。
4. 再跑逐 NUMA 的 BM 小容量分配测试。
5. 最后验证双机读写、Graph 和目标池容量。

节点编号可能变化，应按新的地址报告设置 NUMA 列表。
地址交集变大，只证明有更多可选物理范围。
实际空闲页可结合 MF 的 `mem_scan.py` 估算，再由分配测试确认。
现有 driver 版本号本身不能证明 BIOS 地址调整已启用。

本次还读取了两份官方 Wiki 的 Git 快照。
MemCache Wiki 为 `ba91048806b5`；MF Wiki 为 `6f59c1988553`。
表中的补丁后数字来自官方另一台机器，未冒充 npu1-31 的测试结果。
两个代码仓库及现有 ticket 均未修改。
