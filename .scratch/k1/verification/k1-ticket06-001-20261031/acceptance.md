# K1-ticket06 用户验收确认

2026-10-31，用户明确回复：“验收，并且开始从工程代码中清理测试代码”。
据此接受NPU001的精简功能验收，ticket06 State=closed，user_accepted=true。
09的06依赖已满足；08仍未验收，09不能开始完整验收。

验收版本：`9b760a1addddec05ce2a01d2f2a554a7abe33f67`。
环境：31 P / 32 D，各TP16、GLM-5.2-w4a8、page128、P eager / D Graph。
入口：`d-sharing-acceptance`，run-id `k1-ticket06-001`；
[历史双机命令](../../../../ascend-mempool-test/K1_TICKET06.md)。
日志：`/home/cryang/log/mempool-min/k1-ticket06-001/{prefill,decode}/`。

两轮各6请求，P/D主KV各8份；192份rank回执、逐步内容和写覆盖、
Graph检查、槽复用、读者归零、fail-stop、重启和双端清理通过。
详情与证据层级见[回传核对](README.md)。

接受范围保留既有精简限制：V3独立答案、V8 CPU注册重放；
物理NUMA页驻留和性能未验收，layers及P私有+D共享只有CPU验证。
退出沿用SGLang默认父进程清理；不要求逐rank主动BM释放证据。
原始summary中的user_accepted=false保留，不回写用户报告。

用户同时授权清理验收专用工程插桩。清理后CPU检查另行归档，
此次NPU结果仅对应上述验收版本，不声称已覆盖清理后的代码。
