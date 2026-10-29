# K1-ticket03 Mac验证

基点`e23b57df7d`。以下均由agent在开发Mac执行，不涉及NPU或远端机器。

- 16 rank恢复用例先因缺少分工接口失败，随后通过：轮转/连续分块、1/31/32页、
  零页读者、补齐、实际拷贝量、主KV两缓冲与Index K、重复poll。
- 既有SR延迟最后rank测试接入split_read，提前ack被阻止。
- oracle损坏、报告缺失/错误字节/提前ack、双角色CLI预览通过。
- 完整CPU首轮487项，25项错误均来自沙箱对本机socket、ps或进程清理的权限限制。
  原始输出保存在`full-cpu.log`。
- 获准本机权限后，完整487项在46.543秒内通过，见`full-cpu-approved.log`。
- 审查修正后新增1项定向测试，0/5项策略输出均拒绝，见`review-regression.log`。
  最终集合含488项；未跑第三次完整套件，最后一项只做定向验证。
- 9模块strict mypy通过，见`mypy.log`。
- 14个修改Python文件的ruff、isort、格式及Python3.9语法检查通过。
- 双角色`split-read-acceptance --describe`实际通过，未启动模型。

完整入口：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

## Standards

最终0项硬标准违规，0项需处理的代码异味。
7个功能源文件均位于Ascend/NPU；复用恢复接口、有序tick、全rank回执和验收编排。
默认父进程退出路径不变。

## Spec

初审1项P2：策略对照用zip时，缺失results可能生成空比较并被接受。
已要求两轮cases/results均完整6项，再核对同输入并逐项比较；新增负例通过。
复审剩余0项发现。核心分工、补齐、两类KV恢复、完成时序及接线符合票据。

两个review由独立子agent并行执行，均只读且未重复测试。
NPU/HCCL实际执行、模型结果和硬件生命周期仍待用户运行[双机入口](../../../../ascend-mempool-test/K1_TICKET03.md)。
