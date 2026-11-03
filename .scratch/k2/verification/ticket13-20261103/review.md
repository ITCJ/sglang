# ticket13 两轴评审

基点：`dc31b37cbefbb0b376997fd3949b94a01cb3fdca`。
评审当前工作区的功能 diff 和新文件，在单 commit 前完成。
按 code-review 技能分两个只读 reviewer 并行；两者均未运行测试或修改代码。

## Standards

初审未发现未授权的主线文件、额外 collective、网络线程状态迁移或 shutdown 改造。
修复 prefix 重复序列化、Graph gate 准备阶段跨流依赖，以及 PrefixStore 保存旧 bound method 绕过 collective 标记。
“reset被重复捕获”的初审判断已撤回：model_forward_scope 早于 graph_ctx，begin_forward 的 reset 在 capture 外。
顺序分析各 worker，仅通过 peer 发送检查摘要。
最终复核：0 项未解决发现。

## Spec

初审 6 项：hit/miss 等待误归因、缺 rank/层误报通过、读核账缺失、TTFT 阶段不完整、
Graph 初始化跨流依赖、缺真正 fork 分支。均已修复。
最终复核还补齐 Main 文件、每请求首 token/finished/双侧 free 边界与 Main/worker rid 覆盖检查。
最终复核：0 项未解决发现。

Standards 0，Spec 0 未解决；各自轴内无待修复最高严重项。
NPU replay/task 关联、模型计时、真实字节和用户三档容量数据不在 Mac 静态评审的证明范围。
