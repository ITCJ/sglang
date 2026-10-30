# K1-ticket06 本机验证

基点：`cc14b0518105921dd6320358f84cf912f28a9aeb`。
最终510项CPU用例覆盖收齐，NPU尚未运行。
解释器为`/private/tmp/k1-ticket05-fix-venv/bin/python`。
旧review虚拟环境的torch不可用，改用现有fix环境并补齐测试依赖。

## 实际执行与测试规则

用户在本轮澄清：每轮最多运行两次，失败修正后可以再开一轮验证。
实现初期曾误将其理解为累计两次，导致组合测试暂停等待澄清。
原始日志保留当时的过滤原因；当前没有等待补跑许可的用例。

| 阶段 | 实际结果 |
|---|---|
| 池会话红绿 | 先因缺少decode_shared失败，实现后通过 |
| 共享runtime红绿 | 先缺少模块；接入后CPU夹具漏登记P源而中断 |
| 修复runtime夹具 | features/layers、两rank两步、当前本地KV与完整历史行全部通过 |
| CLI红绿 | 先不支持新模式；接入后两轮入口及D8GiB预算通过 |
| 完整回归 | 发现509项，排除上述3项后运行506项；23个测试方法受环境阻断，产生27个错误含子用例 |
| 定向环境补跑 | 补齐transformers/tokenizers，允许本机socket及ps后，23项全部通过 |
| 评审红绿 | P空区域探针访问和D页驻留报告两项先失败，修复后通过 |
| runtime复查 | 事件快照修正后21项通过；加入上一行损坏且top-k只选当前的失败验证 |

509项原有覆盖与新增1项NUMA报告回归合计510项。
没有重复运行整套，没有减少CPU覆盖。
失败日志与通过日志分别归档，不能把最终覆盖写成首次单轮全通过。
入库文本仅去除行尾空白，结果和报错保持原样。

## 测试入口与证据

`PYTHONPATH`包含`ascend-mempool-test/src`、`scripts`、`tests/unit`。
使用`python -m unittest <module.Class.method> -v`执行各定向检查。
整套使用`unittest.defaultTestLoader.discover`，过滤项写在[full-cpu.log](full-cpu.log)开头。
[retry-cpu.log](retry-cpu.log)记录环境补跑清单与结果。
[runtime-after-review.log](runtime-after-review.log)记录最终runtime验证。

[static-final.log](static-final.log)包含19个Python文件的ruff、格式、isort和Python3.9语法检查。
14个功能模块的strict mypy通过；命令显式包括其类型依赖。
[python-files.txt](python-files.txt)保存本票修改的Python文件清单。

## 双轴评审

Standards初审1项，Spec初审3项；均已修正。
两轴复审各0项未解决问题，见[review.md](review.md)。
CPU的BM/kernel/event替身只证明内容、索引与生命周期逻辑。
真实设备映射、写kernel捕获、跨rank可见性与Graph负测试仍需用户NPU验收。
