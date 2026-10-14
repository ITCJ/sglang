# Ticket02-1：配置与 NUMA 共享计划交付

日期：2026-10-14。审查基点：`d2c999b4faee13e6e8f177c33c5fd847d8de1495`。
本次完成配置和纯 CPU 规划切片；[父票02](issues/02-mla-shared-kv-overlap.md)
保持 `State: open`。实际共享分配、设备映射和服务请求仍待后续切片实现及用户 NPU 验收。

## 修改路径、功能与必要性

| 路径 | 主要修改 | 放在此处的必要性 |
| --- | --- | --- |
| [sharing_plan.py](../../python/sglang/srt/hardware_backend/npu/mempool/sharing_plan.py)（新增） | 严格配置、NUMA 分配、共享组、owner/participant、贡献/probe、端口与逻辑身份规划；P/D 计划校验 | 作为生产 NPU 模块的统一合同，供 CPU gate 和后续 manager 共同使用，避免测试与服务维护两套分组规则 |
| [config.py](../../python/sglang/srt/hardware_backend/npu/mempool/config.py) | `MempoolConfig.numa` 保存配置；服务参数读取；未实现的组启动选项提前拒绝 | 将真实服务输入接入同一配置边界，防止新选项被忽略后继续分配独立池 |
| [manager.py](../../python/sglang/srt/hardware_backend/npu/mempool/manager.py) | 旧 NUMA 解析复用纯函数；两个 BM 入口在初始化前检查共享开关 | 预览与实际分配保持相同节点校验，直接使用 manager 的组件调用也不能绕过阶段限制 |
| [arg_groups/fields/disagg.py](../../python/sglang/srt/arg_groups/fields/disagg.py) | 声明 `--mempool-rank-numa-map` | 当前 `ServerArgs` 从 namespace 字段自动生成参数，必须在此声明才能让正式 CLI 接收文件路径；只增加一个可选字段 |
| [environ.py](../../python/sglang/srt/environ.py) | 注册 `SGLANG_NPU_MEMPOOL_SHARE`，默认 `"0"` | 保持环境变量注册可发现；严格0/1解析放在 NPU 模块，未使用该功能的共享框架无需执行分组逻辑 |
| [verify_numa_plan.py](../../ascend-mempool-test/scripts/verify_numa_plan.py)（新增） | 单命令 CPU gate、拓扑预览、进度、日志及 JSON 报告；失败非零退出 | 提供可独立执行的验证入口，无需 SGLang 服务或 BM SDK 初始化 |
| [test_numa_plan.py](../../ascend-mempool-test/tests/unit/test_numa_plan.py)（新增）、[test_config.py](../../ascend-mempool-test/tests/unit/test_config.py)、[test_pair_startup.py](../../ascend-mempool-test/tests/unit/test_pair_startup.py) | 配置/分组公开合同、旧入口兼容与新选项提前拒绝 | 覆盖1/2/4成员、不均匀分配、非相邻 ranks、多主机、非法输入与 P/D 不匹配 |
| [test_cd_resume.py](../../ascend-mempool-test/tests/unit/test_cd_resume.py)、[NUMA_CD.md](../../ascend-mempool-test/NUMA_CD.md) | 当前版本明确拒绝复用旧 C/D 跨版本 checkpoint，并更新说明 | 生产代码变更使旧固定 runtime 摘要失效；保留原摘要及兼容策略，不把旧测量结果认可为新版结果 |
| [NUMA_PLAN.md](../../ascend-mempool-test/NUMA_PLAN.md)（新增）、[README.md](../../ascend-mempool-test/README.md) | 前置条件、命令、输入格式、失败条件与阶段边界 | 用户可直接运行检查并区分计划输出与硬件证据 |

其中公共主线入口只增加环境变量注册与一个可选 CLI 字段。分组算法和启动约束
位于 `hardware_backend/npu/mempool/`，因为这是后续真实服务会执行的生产模块。

## 实际行为

1. 严格读取 `SGLANG_NPU_MEMPOOL_SHARE=0|1`，默认独立模式。旧节点列表保留
   `nodes[global_tp_rank % len(nodes)]`，在独立模式中无效时继续告警并使用默认分配。
2. 显式 JSON map 使用 `{host, rank, node}` 数组，覆盖本侧全部 TP ranks；节点可重复，
   rank 不可重复。与旧环境列表冲突、主机不匹配、未知/非法节点或缺失 rank 都失败。
3. 按实际提供的 host/node 分配生成共享组；独立模式每 rank 一组。成员排序后第一名
   是 owner。g 成员组有2g个 participants，每侧仅 owner 贡献完整容量，其余贡献0。
4. 每个成员拥有独立64字节 probe，计入贡献与 stride。端口保留独立模式公式，
   共享模式为非相邻成员和不均匀组分配不重叠范围，检查溢出及跨侧同机监听冲突。
5. pool identity 包含逻辑实例、KV identity、成员、layout、pool ID 和 writer 规则。
   P/D 可使用不同物理 host/node，但模式、成员、容量和逻辑身份必须一致。
6. 报告逐 rank 给出 node、group、owner、participant、contribution、probe、端口和
   pool identity。`allocation_status=planned`、`placement_verified=false`；没有测量物理内存。

`instance_id` 和 `kv_identity` 目前由规划调用者提供，真实模型身份获取及 P/D 计划交换
留到02-2。纯计划不拥有 BM handle、设备地址或异步工作，因此没有设备同步和释放动作。
现有 manager 的独立池生命周期继续执行其原有合同。

**正式 mempool 服务在本切片仍拒绝 `share=1` 或显式 map，错误指向 `ticket02-2`。**
这两项新输入可以用于 CPU 预览；默认 `share=0` 且不传 map 的旧启动路径保持可用。
实际零贡献分配、映射、分片写入、Graph、handoff 与共享组关闭不属于本次完成范围。

## Agent 在 Mac 实际执行的检查

- 完整独立 CPU suite：**368/368通过**，日志 `/tmp/sglang-ticket02-checks/full-unit-final.log`。
  首次沙箱运行的16项 socket bind 错误在获准沙箱外运行后消失；唯一代码断言失败为
  旧 C/D release 固定摘要假设，按既有拒绝复用策略更新测试后全量通过。
- 新单命令 gate：**34/34通过**；配置、规划与旧启动入口三组测试，报告
  `/tmp/sglang-ticket02-checks/gate/report.json`，详细测试日志为同目录 `tests.log`。
- CLI 预览：共享与独立模式成功；非法开关、缺失 ranks 均返回1且报告 `failed`。
  证据位于 `/tmp/sglang-ticket02-checks/cli/`。
- 严格 mypy：生产 `layout/sharing_plan/config/manager` 四个文件及新 runner 通过。
  使用项目的 strict/follow-imports=skip 设置；本机 mypy 不再支持配置中的3.9，故临时
  配置以3.10为目标，另用 AST 验证所有10个修改/新增 Python 文件符合3.9语法。
- Ruff lint、format、isort 按对应目录配置检查通过；`git diff --check` 通过。
  公共参数文件按仓库 hook 的 `F401,F821,UP037` 检查，NPU 与测试代码检查 `F,UP037`。

执行环境：`/tmp/sglang-staging-venv`，Python 3.12.14、CPU PyTorch/msgspec。
完整 `ServerArgs` 导入受本机缺少 `triton` 限制，未通过完整 SGLang 启动集成检查。
没有执行 NPU 测试，不能从上述结果推断 BM、Graph、真实 KV 内容或性能通过。

## Standards

并行独立审查未发现硬性规范问题；初次发现一项重复解析逻辑，已让 manager
复用 `parse_numa_nodes()`。复查及附带测试/文档审查无剩余问题。

## Spec

初次发现一项同机 P/D NIC 冲突漏检。新增回归先复现失败，再统一实际端口计算并
补充 host/port 比较；验证同机冲突失败、同机不同端口及异机相同端口成功。
复查及附带测试/文档审查无剩余问题。

审查剩余问题：Standards 0项，Spec 0项。

## 用户运行入口与后续边界

使用现有依赖环境，在仓库根目录执行：

```bash
python3 ascend-mempool-test/scripts/verify_numa_plan.py --self-test
```

默认日志在 `/home/cryang/log/numa-plan/`；Mac 可通过 `--output-dir` 指定新的可写目录。
完整参数及 JSON 示例见 [NUMA_PLAN.md](../../ascend-mempool-test/NUMA_PLAN.md)。

本切片交付配置与规划；父票02的真实服务内容、Graph、物理贡献、并行与释放验收
仍未完成，等待后续实现并由用户执行 NPU 验收，不勾选父票的整体验收项。
