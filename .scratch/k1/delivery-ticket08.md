# Ticket08 交付：八段单份 prefix

目标布局已实现为 `8 / 8 / 1 / 1 / 1`，001两轮NPU精简验收通过，用户已确认关闭08。
11已解锁，尚未实施。规格沿用 [实施计划](ticket08-implementation-plan.md)与 syncfile 最新票据。

一个新请求记录一次起始段，admit 按段交错选页，段满时选剩余页最多的段。
八个 owner 从各自 alive 主 KV 副本和单份 Index K 保存本段；单 rank 对照可配置。
分片持有设备映射，按五页层步长解析段地址，继续使用原来的索引拷贝。
SR 核对单写者与全部完成事件后，Main 才按父到子发布每页一个位置，随后释放源。

命中时按真实段分配读者，主 KV/Index K 各用一个汇总缓冲，all_gather 后恢复原生非连续页。
零页读者仍参加通信，但 padding 不读取 DRAM。fill 直接从全部段填回八份主 KV 和单份 Index K。
writer 按完整 prompt 划分；全部 fill 与后缀写回完成后才能 ready，原有源保护与 free_ack 保持。

共享校验按两类数据分别汇总实际源写者的摘要，正常 HBM 恢复与该独立期望比较。
只有验证通过的恢复数据可成为下一请求的继承参考；没有新增请求级 BM 读回。

## 功能源码阅读顺序

全部为 Ascend/NPU 专用源码；通用主线功能源码净改动 **0**。

| 文件 | 用途 |
|---|---|
| [mempool_router_protocol.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_protocol.py) | 共享配置、默认策略、单份副本数和注册策略 |
| [prefix_layout.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix_layout.py) | 段页编码、交错选页、耗尽回退和任务配对 |
| [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py) | 八段物理贡献、零贡献 follower、独立探针槽与规划报告 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 按段 view、实际层步长、设备地址范围及注册信息 |
| [split_read.py](../../python/sglang/srt/hardware_backend/npu/mempool/split_read.py) | 按描述符实际段分配读者 |
| [prefix.py](../../python/sglang/srt/hardware_backend/npu/mempool/prefix.py) | 分段 admit/load_back/fill、事件完成、单写者及真实源校验 |
| [runtime.py](../../python/sglang/srt/hardware_backend/npu/mempool/runtime.py) | 将会话与策略接入 PrefixStore |
| [mempool_subrouter.py](../../python/sglang/srt/disaggregation/ascend/mempool_subrouter.py) | 全 rank 同步分配、完成汇总与共同描述符 |
| [mempool_router_transport.py](../../python/sglang/srt/disaggregation/ascend/mempool_router_transport.py) | 注册有效布局和策略，连接现有运行时 |
| [mempool_router.py](../../python/sglang/srt/disaggregation/ascend/mempool_router.py) | 单位置匹配/发布、逻辑额度与源释放门控 |

## 脚本、测试与说明

- [run_mempool_min.py](../../ascend-mempool-test/scripts/run_mempool_min.py)：两轮六例入口、准确 BM 预算、与 07 实验模式解耦。
- [verify_mempool_shared.py](../../ascend-mempool-test/scripts/verify_mempool_shared.py)：分段注册、实际页集合、按段行数及字节核对。
- [verify_mempool_prefix.py](../../ascend-mempool-test/scripts/verify_mempool_prefix.py)、[verify_mempool_reuse.py](../../ascend-mempool-test/scripts/verify_mempool_reuse.py)：源写者与单位置目录、正常恢复内容核对。
- [verify_mempool_session.py](../../ascend-mempool-test/scripts/verify_mempool_session.py)：精简验收汇总和六/九例模式边界。
- [verify_numa_plan.py](../../ascend-mempool-test/scripts/verify_numa_plan.py)：接受 serving 配置，预览相同八段几何。
- 测试位于 [tests/unit](../../ascend-mempool-test/tests/unit)：新增 `test_prefix_shared.py`，扩展池、Main/SR、内容证据、注册和入口测试，复用现有 CPU copy/event/collective。
- [NPU 命令与覆盖范围](../../ascend-mempool-test/K1_TICKET08.md)、[本机验证记录](verification/ticket08-cpu-20261101/README.md)。

## 验证与后续

532 项 CPU 合并覆盖通过：一次完整运行，受沙箱阻断的 21 项单独补跑通过。
24 文件静态检查、18 模块 strict mypy 通过；[Standards/Spec评审](verification/ticket08-cpu-20261101/review.md)均0项发现。
功能只提交当前分支的本票改动，已有其他工作区改动保留。

NPU 验收由用户运行，每机一条命令。默认两轮均使用 owners admit；single_rank 对照当前有 CPU 证据。
物理 NUMA 驻留、性能、直接读取八份 fill DRAM 内容未验证。V3 用独立答案、V8 用注册 CPU 重放；
这套入口是精简功能验收，不宣称原始 V1–V10 全覆盖。需用户确认后才关闭 08、解锁 11。

最新状态及保留边界见[用户验收确认](verification/k1-ticket08-001/README.md)；下方原交付期验证说明保留为历史记录。
