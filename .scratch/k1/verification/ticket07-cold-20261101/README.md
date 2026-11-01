# K1-07 冷对照诊断补丁

用户在420270d493运行k1-ticket07-002，fetch通过；第1轮9请求中仅index-update答案失败。
输出为代码围栏中的pencils=12，预期17，26 tokens正常EOS，hit_len=4096。
其余8例通过，logic-a与index-full-hit输出17 tokens完全一致。
用户离线check_reuse通过：16 ranks、78层main、21层Index K，源快照与正常恢复内容一致。
以上来自用户反馈，不是开发机直接执行硬件测试。未直接读取BM，尚不能判定根因。

## 补丁与验证

不改模型功能源码、不改提示词/期望答案/JSON解析规则。
run_mempool_min.py增加诊断入口、保持共享布局、禁用匹配、复用进程清理与端口检查。
mempool_index_diagnosis.py读取已保存失败例，提交原始input_ids与sampling_params，
保存原始响应、快照及原/冷输出对照，答案失败在双机清理后返回非零。
这是同一mempool布局下的无命中对照，不是无mempool正式服务基线，也不替代ticket验收。

CLI配置检查先失败后通过；完整定向31项回归一次通过（cpu.log），覆盖原验收合同。
4文件ruff/isort/Python3.9 AST通过，新增模块strict mypy通过。
Standards与Spec独立评审各0项需修复；硬件诊断未运行，07仍open。

后续用户已运行冷对照并确认07验收，见[关闭记录](acceptance.md)。上文为诊断补丁交付时状态。
