# Ticket11 工程部分：Mac 检查记录

日期：2026-11-02；差异基线 `746ec2d54c5fa015a891e5e9b94fc2d47251d783`。
本地 Python3.12，复用独立 ascend-mempool-test 环境。NPU/模型服务/SDK 全量构建均未运行。

## 实际运行

- `unittest.log`：完整测试一次，548 项；1 failure、25 errors。25 errors 来自21个受本地 socket/子进程沙箱限制的方法。
- `retry.log`：仅这21方法在允许本地 socket/子进程的环境补跑一次，全部通过；未第三次运行。
- 合并完整套件为547/548通过。剩余旧 `test_config.TestMempoolConfig.test_defaults_and_unsupported_model_storage`
  期待超4GiB的decode区域触发 UINT32_MAX 错误，与票12已存在的分窗实现不符；本票未修改该用例或相关行为，不计为通过。
- `checker.log`：后增服务验收检查器回归1项通过，验证有效日志通过、缺失50ms延迟被拒绝。
  与上述不重叠；合并唯一方法总数549，548通过、1既有失败。
- `sdk-check.txt`：GitCode v1.1.4 的 BatchCopyG2G 函数原文提取体，C++ 桩模拟首/末子提交失败和全部成功；原版退出1、补丁版退出0。
  只证明该错误传播分支，不证明完整 SDK 编译、加载的二进制或硬件行为。
  另以独立临时目录中的tag原文件执行 `git apply --check` 通过，未改外部SDK checkout。
- 静态检查：修改 Python 文件 ruff、AST，适配层及 prefix/地址依赖共8模块 strict mypy通过；验收入口 `--help` 通过。

运行入口：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts python -m unittest discover -s ascend-mempool-test/tests/unit
```

## Standards

独立评审未发现阻断性标准违规。功能修改均在 Ascend/NPU 目录；状态/集合通信留在 tick，
执行线程只做池库提交与等待；沿用父进程清理。建议改清短局部变量名称已完成。

## Spec

独立评审发现验收检查器最初缺失强制双操作覆盖、完成到receipt顺序与强制延迟验证；均已补齐。
检查器对照 Main 历史核页数，强制16分片、2N/9N、字节总数、提交/库完成/receipt顺序及实际50ms延迟。
真实 SDK 在途失败/超时未执行硬件验证，作为未满足的验收证据保留；不声称完整规格已验收。

## 交付状态

工程代码、可执行验收入口与 SDK 补丁已准备，等待用户在修复 SDK 上运行两轮服务验收。
服务不新增目标读回；内容证据边界遵循用户此前确认，详见 [工程交付](../../../../ascend-mempool-test/K1_TICKET11.md)。
11保持open，09仍阻塞。
