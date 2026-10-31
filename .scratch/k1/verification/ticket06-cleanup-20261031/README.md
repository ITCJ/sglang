# K1-ticket06 验收后工程清理

用户2026-10-31确认NPU001精简功能验收并授权清理，基点9b760a1add。
[人工验收确认](../k1-ticket06-001-20261031/acceptance.md)与原始user_accepted=false分开保存。
本次只清理ticket06新增的临时工程插桩；其他票既有验收路径仍保留。

删除启动Graph延迟写者负测试、逐层上一token额外DRAM读回、设备计数、
完成快照和回执字段、verify_decode开关及专用d-sharing-acceptance入口。
正常共享分写、完整当前KV本地缓冲、历史miss读取、原有stream/event与释放门控保持。
BM会话身份标记仍用于确认真实对端，属于运行握手，不是本次被删的验收探针。

当前serve/session-acceptance继续支持配置decode_shared。
通用回归不再声称覆盖D逐步哨兵或延迟负测试；旧验收可在9b760a1add独立checkout复现。
独立离线核验器保留旧报告与负例检查，不再由当前服务入口调用。
当前验证仍要求真实P/D miss源读取；没有用虚构的哨兵计数补足。

## 本机检查

11个Python文件ruff、格式、isort和Python3.9语法检查通过；13模块strict mypy通过。
[静态记录](static.log)，[文件列表](python-files.txt)。
完整CPU单轮510项通过，48.412秒，见[CPU日志](full-cpu.log)。
Standards与Spec两轴复审均0项未解决问题，见[评审记录](review.md)。
没有访问NPU；001硬件结论仅属于9b760a1add，未宣称清理后已获NPU验收。

## 覆盖调整

共享decode组合用例保留features/layers、两rank两步、完整当前token、历史整行、padding和完成记账。
删除子场景：top-k未选中上一token时由额外哨兵发现损坏，以及已撤销的哨兵计数断言。
CLI用例调整为通用session-acceptance，仍核对D共享和8GiB预算。
旧Graph正负结果检查移至独立历史报告核验器，保留反例覆盖。

## 检查命令

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/k1-ticket05-fix-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

本机测试需要回环socket及进程检查权限，已在沙箱外执行；不连接NPU。
