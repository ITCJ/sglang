# Ticket04 实施记录

基线：`7bce684429`；2026-10-27 用户授权实施本票。
现有工作区文档修改单独保留；本票保持 open，等待自己的用户 NPU 验收。

## 已确认边界

沿用 spec「已确认的测试边界」和 ticket04 Verification：真实 Main→P/D、
CPU service／TP tick、NPU 复制适配器，以及自运行入口的报告判定与进程所有权。
CPU 使用可控的外部 tokenizer、HTTP、设备完成和进程边界，不替代硬件验收。
代码 review 固定比较实施前的 `7bce684429` 与本票变更。

## 实施顺序

1. 可读逻辑题、真实 tokenizer 精确长度、独立答案与输出差异报告。
2. 复用既有内容检查，扩展命中验收到 ABCE，严格汇总 V1–V10。
3. 同一入口接入完整模式，隔离参考、目标、长生成／早结束及故障运行。
4. 补齐注册容量拒绝和失败报告，验证双机日志、清理与退出码。
5. 相关 CPU／静态检查、完整 CPU 套件、Standards／Spec review、交付与提交。

每步先通过公共边界写失败测试，再实现；立即同步目标页 D。
不修改共享存储设计或数值计算路径。

## 文件范围原则

运行服务若需修改，仅优先使用 `disaggregation/ascend/` 或 `hardware_backend/npu/`。
既有 `ascend-mempool-test/scripts/` 负责双机启动、fixture、离线证据与报告。
这些服务外行为需要由独立入口负责，不能由模型内 Ascend/NPU hook 完成。
其他主线功能修改逐文件说明原因；当前尚未发现此类需求。

## 验证状态

- 实施：逻辑ABCE、独立答案、完整证据、隔离容量/超时及双机入口已接线。
- CPU／静态：完整469项测试通过；9文件lint/format/isort/3.9语法及7模块strict mypy通过。
- 最终检查：Standards与Spec复核均0项未解决；超长拒绝和稳定性诊断修复已包含在完整回归中。
- NPU：尚未运行，由用户执行；旧报告不替代本票完整验收。

实际检查记录见[verification/ticket04-cpu-20261027/checks.json](verification/ticket04-cpu-20261027/checks.json)。
