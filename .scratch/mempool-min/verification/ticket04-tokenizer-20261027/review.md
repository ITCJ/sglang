# 两轴审核：ticket04 tokenizer修复

基线：72fd610af89f038f8c41311a342d132585c9ac25。
范围：本次脚本一行修复、真实Transformers回归、验收说明与证据。
审核检查工作区diff，预存用户改动不属于本次审核。

## Standards

0项发现。修复局限于独立Ascend验收工具，保留严格IDs校验。
真实API测试覆盖完整长度与精确前缀，无网络或权重依赖。
未发现文档规范违反或需处理的Fowler代码异味。
失败记录、CPU通过和待用户NPU重测区分清楚，票保持open。

## Spec

0项发现。return_dict=False消除把BatchEncoding字段名当token IDs的误比较。
规格要求的4096/5120长度、B精确前缀和A/C相同IDs继续验证。
真实API回归完成red/green，公开GLM-5.2六个输入通过。
无缺失要求、错误实现或范围扩大；001失败和V1–V10 not-run保留。

Standards：0项；Spec：0项。NPU重测待用户执行。
