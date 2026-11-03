# Ticket13：fork 已知模型答案问题的验收修正

基点：`a762620e6f`；用户实机 `k2-ticket13-003`。
用户已明确本题17→12为此前排查的量化模型精度问题。
本次沿用该接受边界，修正重新阻断已知问题的验收脚本。

## 证据与接受边界

003的新Graph检查、页快照与padding通过；参考A/C的17个输出token一致。
fork输出代码围栏中的pencils=12，26 tokens正常EOS，严格JSON检查failed。
这是[K1-07已接受的同一题和结果](../k1/verification/ticket07-cold-20261101/acceptance.md)。
当时冷/热26个输出ID完全一致；用户接受其为模型答案问题而非本票系统实现错误。

本次实现将该旧题用于fork观测，却重新以严格答案作为阻断条件，未沿用此前接受边界。
没有证据要求重新诊断量化机制、修改题面或改写期望答案。

## 新判定口径

- 原生参考：保存完整输出，status=reference-recorded，answer.status仍为failed。
- 目标预热和测量：完整输出token IDs必须与本轮原生参考一致；差异返回failed并保留首个差异位置。
- 输出结构、实际长度、EOS仍检查；缺失参考或输出也不能通过。
- off/on的fork输出ID比较沿用已有入口。
- 原有六例逻辑答案、内容与生命周期检查不变。

`observation-fork.json`分开保存运行比较状态与严格答案诊断。
目标status=passed只代表与参考一致，不宣称模型答对了17。
同一服务追加fork，不增加模型启动次数。

## 本票净改动清单

Ascend/NPU功能源码：无。通用主线功能源码：无。

脚本与测试：

| 文件 | 用途 |
|---|---|
| `ascend-mempool-test/scripts/run_mempool_min.py` | 保存本轮fork参考；统一生成比较报告；仅以目标/参考完整ID差异阻断该fork |
| `ascend-mempool-test/tests/unit/test_mempool_min_entry.py` | 真实Runner.stage路径复现并修复已知输出阻断；验证答案failed仍保留、相同ID通过、不同ID失败、缺失输出失败 |

说明文档：本页、issue、memory、target-alignment D、验证记录。

## 验证

新集成测试先重现 `ValueError: observation fork answer failed`。
首次沙箱禁止进程表查询，清理报权限错误；第二轮使用本地进程权限。
9项runner/fixture回归与4项原有严格答案测试全部通过，同一测试最多两轮。
未重复完整套件，未访问NPU。ruff/isort/Python3.9 AST及runner严格mypy通过。
测试替换外部服务为本地自有子进程，真实进程所有权和清理路径保留。

记录：`verification/ticket13-fork-baseline-20261104/checks.json`。
完整硬件验收仍待用户运行，13保持open，不解锁18/21/22。

## 双机重测

沿用原NPU环境、CANN9.0.0和配置，两机先更新代码。
31先启动，32随即启动，统一新run-id `k2-ticket13-004`。

```bash
python3 -u ascend-mempool-test/scripts/run_k2_observation.py --role prefill --config /home/cryang/mempool-min-ticket01-3132.json --run-id k2-ticket13-004 --sdk-build-id mf114-c01f3ad-ticket11-batchfix
```

```bash
python3 -u ascend-mempool-test/scripts/run_k2_observation.py --role decode --config /home/cryang/mempool-min-ticket01-3132.json --run-id k2-ticket13-004 --sdk-build-id mf114-c01f3ad-ticket11-batchfix
```

日志根目录 `/home/cryang/log/k2/k2-ticket13-004/`。
预期先越过参考fork阶段，再完成目标预热/测量与完整矩阵。
最终仍需两侧报告passed_current_configuration；目标ID差异、功能或观测完整性失败仍非零退出。
