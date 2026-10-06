# Ticket03 S6.3：三个 Standards 整改

实现基线：`912012f3d0`；分支：`cryang/dev/mempool`。用户2026-10-06授权按
删除统计 → 共用row推导 → control查询/TP同步顺序实施、分别验证和提交。
范围及约束见[S6计划](ticket-03-s6-plan.md)。本地检查不替代最终NPU验收，03保持open。

## 1. STD-03：删除fetch调试统计

生产runtime不再计算或累计selected KV、cache hit、P miss和D miss。
`_fetch_checks`由六列缩为三列，保留层覆盖、非法读取数量及错误位置；每forward独立
完成快照、事件顺序、binding、可读范围和故障禁止释放仍保留。

`completion_report()` / `mempool decode_completion`仅传递Graph、绑定、写入完成及
drain事实；服务checker同步改用新日志，不再要求hit/miss为正。独立fetch gate仍通过
独立pattern逐元素验证P/D miss、hit、padding及连续异步replay，保留实际copy覆盖。

验证边界是已有runtime完成、service安全释放及正式日志checker公开接口。
先迁移checker fixture，原实现报incomplete attempt；修改实现后27项针对性CPU测试通过
（service_gate、fetch、fetch_layers、pd_service）。严格mypy 33文件、Ruff规则和format、
diff空白检查通过。首次mypy命令漏传测试src目录导致导入失败，补齐标准调用后通过。

## 后续验证

最终版本使用[正式服务命令](../../ascend-mempool-test/FORMAL_SERVICE.md)执行fetch gate、
zero/decode/reuse请求及服务checker，普通模式按同页native命令回归。真实NPU未执行。
