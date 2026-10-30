# K1-ticket03 验收后清理

用户2026-10-30确认NPU006通过并授权清理；代码基点0337b9b9ad。
人工确认与原始自动报告分开保存，见[验收确认](../k1-ticket03-006-20261030/acceptance.md)。
本次删除临时计时、工程证据状态/日志、gather额外比较及依赖它们的当前专用启动入口。
功能核、共享槽位表、两块缓冲、恢复stream、event和全rank回执保留。
原有计算源/native HBM内容验证保留，继续不额外读BM。
文件清单及当前/历史入口区别见[最终交付](../../delivery-ticket03.md)。

## 本机检查

12个修改Python文件的ruff、格式、isort和Python3.9语法通过；9模块strict mypy通过。
完整CPU单轮492项通过，48.091秒，见[full-cpu.log](full-cpu.log)。
最终静态检查见[static.log](static.log)；离线核验器最后只补充归档用途注释，无行为改动。
Standards与Spec两轴只读复审均0项发现。
没有重跑NPU；006硬件报告只对应清理前0337b9b9ad。

本次删除2项只针对临时计时编译顺序和旧专用CLI的测试。
内容/流量/补齐/最后rank完成门控检查沿用原用例；gather损坏通过恢复后native核对发现。
保留旧报告核验器及反例，用于历史证据，不再要求现行工程输出已删除的探针字段。

## Standards

0项硬规范违规，0项需处理代码异味。
功能改动限定Ascend/NPU；默认退出未改，符合AGENTS.md边界。
实际内容/复制量/补齐及最后rank门控测试保留，只移除两项已失效的临时探针测试。
八个整票净功能文件与diff一致，脚本/测试/文档单列。
人工确认、原始user_accepted=false及清理后未重跑NPU分别记录。

## Spec

0项发现；最严重问题：无。
H2D、all_gather、scatter仍在原恢复流中按序提交，索引/表/缓冲持有至event完成。
SR仍收齐全rank回执后ack；原生HBM与独立源快照核对保留。
gather额外CPU比较只在旧验收开关开启时执行，默认生产路径原本不依赖它。
撤下专用入口后session/serve路径完整，离线核验器与0337b9b9ad历史复现保留。
未发现缺失清理、功能误删或超出约定范围的改动。

两轴未执行额外测试；本次完整CPU只运行一轮。

## 检查命令

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

保留历史版本和验收报告；没有重写已发布提交或修改baselinetest。
