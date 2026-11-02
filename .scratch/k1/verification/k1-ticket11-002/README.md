# Ticket11 工程002：用户确认验收并关闭

2026-11-02，用户回传31/P的启动输出与最终JSON，原样字段见prefill-user-report.json。
运行入口run_pool_copy_acceptance.py，run-id k1-ticket11-002，配置/home/cryang/mempool-min-ticket01-3132.json。
SDK标签mf114-c01f3ad-ticket11-batchfix；用户此前确认两侧Docker实际安装过补丁。
指导更新版本为bad113345e；当前摘要没有commit字段，未额外采集远端HEAD，不把指导版本写成独立核实事实。

## 已有证据

- status=passed：P基础服务验收返回0，额外pool-copy检查通过。
- target与restart每轮4操作、64分片记录，即每操作16分片；总8操作、128记录。
- 按该入口代码判据，admit/fill覆盖、Main历史对照、2N/9N块数及字节、提交→库完成→SR receipt、
  非空任务50ms延迟和至少两次poll均通过；此处是依据检查器成功作出的结论，尚未单独审阅原始trace。
- 两轮服务运行走完，001的setup OSError22/HTTP500现象在这次运行未再出现。
- 不把此结果扩大为真实硬件在途故障注入、性能等价或生产目标全量读回证明；原证据边界保留。

## 32侧结果与当前状态

用户已回传32/D最终JSON，status=passed，SDK标签与P一致，见decode-user-report.json。
D基础验收通过；额外传输证据由P报告承担，这是入口设计的正常分工。
双侧自动验收结果已收齐；用户于2026-11-02明确回复“关闭”，接受当前证据边界。
原始JSON中的user_accepted=false是运行时保留标记，原件不改；后续人工确认记录在本文。
11已closed，09的06/08/11依赖均满足（12也已closed），09已解锁但尚未实施；无需重跑现有验收。
日志根目录/home/cryang/log/mempool-min/k1-ticket11-002/{prefill,decode}/。

## 关闭结论

用户在收齐双侧报告及获知证据边界后明确确认关闭。接受范围为：demo独立设备读回、
002服务target/restart两轮与搬运检查、CPU失败传播回归及默认清理路径。
未验证的真实SDK在途故障/超时、忙闲tick性能等价、生产目标全量独立读回仍保持未验证，
不因关闭改写为通过。功能提交746ec2d54c、d80ec2b2e1，绑核修复bad113345e；远端HEAD未额外核实。
