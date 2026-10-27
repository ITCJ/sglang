# ticket04-002：用户回传完整 NPU 验收通过

2026-10-27，用户从 npu1-31 回传 prefill 终端输出。
入口为 full-acceptance，配置为 /home/cryang/mempool-min-ticket01-3132.json。
原文见 [user-terminal.txt](user-terminal.txt)，提取的原始摘要见 [terminal-summary.json](terminal-summary.json)。
这是用户执行 NPU 测试的反馈，不是 agent 在开发 Mac 上运行硬件测试。

| 项目 | 用户回传结果 |
|---|---|
| 总结果 | passed |
| V1–V10 | 十项全部 passed |
| 同条件冷算 A/C 稳定性 | passed；各17个输出IDs，完全相等，无首差 |
| 长生成 | passed |
| 超时 fail-stop | passed |
| 双端清理 | passed |

终端显示 fetch、reference、target、capacity、timeout 依次启动。
实际阶段与全部rank明细由远端报告保留，本地仅收到终端摘要。
日志根目录：/home/cryang/log/mempool-min/ticket04-002/。
完整报告：prefill/v1-v10.json；入口总报告：summary.json。
最新交付修复为3da1fd61be；本段终端未显示git版本，以两侧identity.json为实际版本证据。

本次稳定性相等只指参考组冷算A/C，不能推出所有参考/目标输出IDs相等。
V3按独立标准答案验收；不改写旧ticket03合成C失败或物理NUMA页落点未验证的边界。
本次证明范围是sparse阶段0/L0，不包括K1、后续阶段或dense。

2026-10-27用户明确回复“关闭”，确认接受本票实现与ticket04-002完整NPU验收。
State=closed，Status=ready-for-agent，user_accepted=true。
[用户确认记录](user-confirmation.json)与原始自动化摘要分别保存。
此前ticket04-001输入生成失败和tokenizer修复的证据继续保留。
本轮仅更新记录与页面，无源码变更，未重跑CPU或NPU测试。
