# 14: K2-M2 带宽刻画套件（不经服务）

**What to build:** 把票 04 扩成一套不经服务的小程序，用服务同一套映射、page first 区域布局和拷贝 kernel（UNIDEX / 多基址），按档位测：1 / 2 / 4 / 8 / 16 个 GPU 读同一份与 16 个按段交错；token 行（1152 B 随机行，top-k 2048 × bs 16 一层的量）对页-层块 147,456 B 对整页块 11,501,568 B；本 NUMA 对本机他 NUMA 对远端机器（P↔D），读与写各一张；同一 NUMA 叠加 write_through 式顺序写（一层 151 MB）时读曲线的变化；P→D 远端写吞吐与延迟，带与不带 D 本地读，一层 prompt_len × 1152 B 在 4K / 32K / 128K 三档；小读对比 Mooncake get（切片约 1 KB / 4 KB / 16 KB / 147 KB，几千个 / 步，ops/s、CPU 核、p99；部署不了就记原因只测我们的路径）；页驻留抽样。每档 3 次取中位数，记每读者时间、总带宽、每段带宽。结论回填 U6，回答 Q24 / Q27 / Q7 / Q5，给票 17 预算、票 19 阈值；远端写一项是票 25 的开工条件。细节见 k2-tickets.md 第 6 节。

**Blocked by:** None (can start immediately; 与 [13](13-k2-m1-observability-mode.md) 并行). 远端写一项 blocks [25](25-k2-p1-dual-write.md)。

**Status:** ready-for-human

**State:** open

- [ ] 每档三次重复的中位数表与曲线：读者数、粒度、位置、带写流、远端写、小读、驻留各一组
- [ ] 结论写明：一份线性到几个读者、交错相对单段的倍数、远端对本机的倍数、远端写能否藏在一层计算后面
- [ ] kernel 与映射与服务同版本并在报告注明
- [ ] U6 回填；Q24 / Q27 / Q7 / Q5 各有一句答案；票 04 关闭并注明并入

## Comments

### 2026-11-03：同步设计侧交接

源稿 Status 注记：ready-for-agent（2026-11-03 加票；票 04 并入）。
正文与验收项按源稿导入；State=open。代码未实施，CPU/NPU 验收未运行。
当前可开工项、数据门槛和源稿冲突按[本地 spec](../spec.md)的同步核对记录执行。

### 2026-11-05：代码交付，等待用户NPU验收

用户调用implement授权实施，采用本会话补充的8K、双写竞争、连续逐层队列与统计口径。
复用生产布局/映射，工程改动仅独立微测目录；未改推理服务或通用shutdown。
入口与31/P、32/D命令见 [BANDWIDTH.md](../../../ascend-mempool-test/BANDWIDTH.md)。
全部功能阅读顺序见 [交付说明](../ticket14-delivery.md)。

新增6项关键CPU验证通过；不同测试方法592项中586通过、6项既有失败，已在50395d0b3b复现。
其中4项新测试沿用已完成的红绿结果，末轮不重复；原有套件与双机入口共588方法仅运行一轮末检。
Ruff、7模块strict类型检查及shell语法通过。Standards/Spec最终未解决项均0。
结果见 [检查记录](../verification/ticket14-20261105/checks.json)。

尚未运行NPU，全部实测验收项保持未勾选，State=open。
remote-write是优先交付的子范围；full才涵盖完整矩阵。Mooncake不可部署时自动保留真实原因。
层窗口使用对齐首操作的全写者完成时间，不把传输微测视为实际计算隐藏证明。
25仍等待远端写实机证据与多基址kernel；未提前解锁。
K1-04此前已转归关闭，本轮未重复关闭或追认硬件验收。
