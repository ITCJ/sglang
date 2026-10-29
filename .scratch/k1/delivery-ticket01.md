# K1-01 代码交付

实现与 NPU 验收分开。本次代码交付保持 ticket open，未执行 NPU 测试。
操作命令和判据见 [独立验收说明](../../ascend-mempool-test/K1_TICKET01.md)。

## 功能源文件阅读顺序

1. `python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py`：完整映射校验、拒绝码、注册与计划的 applied 核对；保留 prefix/Index K 布局。
2. `python/sglang/srt/hardware_backend/npu/mempool/config.py`：生产启动配置在 BM 前验证本侧 host 与节点。
3. `python/sglang/srt/hardware_backend/npu/mempool/runtime.py`：将已验证的 NUMA 配置传给原有 rank-pair 初始化。
4. `python/sglang/srt/hardware_backend/npu/mempool/manager.py`：原有 create2 接受指定节点；生成本地 owner 与区域证据。
5. `python/sglang/srt/hardware_backend/npu/mempool/placement.py`：只查询页节点和本池 VMA 的驻留计数。
6. `python/sglang/srt/disaggregation/ascend/mempool_router_transport.py`：SR 注册附带 NUMA 证据，Main 的逻辑消息与账本不变。


以上顺序不含测试代码、README 和解释文件。
每 rank 保有原有私有池；没有新共享参与者或新同步协议。
P 在原有 ready 条件齐全后发布；D 读完并 drain 后，按原有协议释放 P 槽。
驻留查询仅在显式映射的启动注册阶段执行，不进入请求或 Graph replay 路径。

## Ascend/NPU 目录外改动原因

- `ascend-mempool-test/scripts/` 是仓库约定的独立验收工具目录。
  runner 必须协调两个容器、模型进程和报告；运行时 NPU hook 无法承担进程外的验收编排。
  `verify_numa_plan.py` 将新增测试加入既有自动 CPU gate。
- `python/sglang/srt/arg_groups/fields/disagg.py` 仅更新现有参数的帮助文字，移除过时的 planning-only 描述；没有新增共享功能路径。
- 按用户2026-10-29指令，当前分支移除基线perf工程代码及测试入口。
  公共output_streamer恢复到导入前；全部性能代码保留在baselinetest分支。
  当前分支仅保留 `.scratch/mempool-baseline/verification/` 中的结果记录。

## 验证记录

- TDD 边界由用户确认：规划器/启动配置、注册驻留报告、自运行验收 CLI。
- 功能阅读基点：`8869c2c605`；本轮移除对照原K1提交`3156916a11`。
- 旧 `verification/ticket01-cpu-20261029/` 是移除前记录；当前检查另存于 `verification/ticket01-no-perf-20261029/`。
- 实际页驻留、BM 与模型 Graph 行为、两种映射的 V1–V10 仍待用户 NPU 功能验收；性能复测在测试分支另行安排。
- SDK 没有独立 GPU 注册计时接口；报告保留 null。是否需要其他驱动证据待硬件验收核对。

移除后的实际检查：完整476项CPU、735子测试；定向24项、103子测试通过。
14文件静态检查、7模块strict mypy及双角色CLI预览通过。Standards/Spec均0项。
31/32命令已更新：保留numa-acceptance，删除旧命令的--perf-rounds参数。
