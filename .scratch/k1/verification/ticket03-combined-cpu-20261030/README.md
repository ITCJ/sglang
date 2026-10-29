# K1-ticket03合并缓冲：Mac检查

修订基点`87e802de03`，整票基点`e23b57df7d`。
本轮由agent在开发Mac执行；没有访问NPU或修改baselinetest分支。

- `red.log`：旧路径不满足in-place输入/输出同存储断言，6个子场景失败。
- `restore-cpu.log`：改写后11项恢复检查通过，覆盖内容、生命周期和私有副本反例。
- 首轮完整CPU490项通过，46.386秒，`full-cpu.log`。
- 评审修正后最终完整CPU491项通过，46.697秒，`final-cpu.log`。
  本次完整套件两轮；最后增加1项编译时序验证。
- 最终10模块strict mypy通过，`mypy.log`。
- 13个修改Python文件的ruff/isort/格式与Python3.9语法通过，`static.log`。
  Triton核没有运行mypy；其源码语法、单次launch包装和地址语义分别检查。
- 双角色CLI预览通过：`prefill-preview.json`、`decode-preview.json`。

CPU新增覆盖包括：16rank的in-place别名、真实page128下1/31/32页、轮转与连续块、
两块buffer地址、同一slot表、31页补齐跳过、非连续原生页，以及实际复制字节。
散开核的真实Python AST使用CPU指针/Triton语言替身执行，flat/NZ读回由原生pool方法核对。
这证明地址表达式和掩码语义，不能证明Triton-Ascend编译器或HCCL硬件支持。

完整CPU命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

## Standards

0项硬标准违规，0项需处理代码异味。
功能范围在Ascend/NPU；复用既有恢复stream、SR完成门控和默认进程退出。
临时计时按本次用户明确授权处理，不套用旧测试分支限制。

## Spec

初审1项P2：首次Triton JIT编译可能被计入scatter设备区间。
已在timing.start前warmup编译及加载，实际scatter仍一次发射；CPU边界测试证明调用顺序。
复审剩余0项阻塞。完成时刻和T_lb_rank在观察到event完成时采集，先于后续HBM校验。

两轴由独立子agent只读评审，没有重复执行测试。
性能表是带V2的诊断数据，不能等同生产基准；旧基线四段不可补造。
NPU验收、变差原因分析和临时工程测试代码清理仍待用户运行后推进。
