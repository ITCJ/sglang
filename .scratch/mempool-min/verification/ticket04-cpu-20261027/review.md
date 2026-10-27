# Ticket04 Standards / Spec review

固定基线：7bce6844299f4d6f299dbd4cd066d2167728a4e8。
实现提交前审查暂存diff：`git diff --cached 7bce684429 -- ascend-mempool-test python`。
两轴由独立sub-agent并行执行；本轮未运行NPU，ticket保持open。

## Standards

首次：无功能代码规范阻塞；发现实施记录的旧状态与已完成实现/CPU检查冲突。
修复：更新实施记录，明确代码、CPU、完整回归与NPU状态。
复核：0项未解决。后续Spec修复也完成Standards复核，仍为0项。
Ascend运行代码边界、独立工具必要性说明、既定测试边界与硬件门控均符合要求。

## Spec

首次发现两项P2：

1. 超长输入8256也未按128对齐，可能只测到对齐拒绝。
   修复为容量加一页，即8320；HTTP边界测试移除长度保护时能独立检出。
2. 同条件的冷算参考A/C没有单独比较。
   新增stability.json，记录IDs长度、相等与首差；异常和缺失均阻止完整通过。
   此结果与跨形状复用差异单独报告。

复核：0项未解决。两项修复的red/green及完整469项CPU回归均保留。
硬件接受不由静态审查、CPU或旧票结果替代，仍需用户执行完整入口并确认。
