# Ticket26 本地检查

基点：`aa5852624e15df139df7c76bb8d757341b049713`。日期：2026-11-06。
这些记录只证明本地实现、CPU 检查和代码评审，不代表设备验收通过。

| 记录 | 内容 |
|---|---|
| checks.json | 汇总；26 保持 open，25 不解锁 |
| phase1-source.tar.gz、phase1.json | 一对一替换阶段的源码覆盖包、基点及摘要 |
| phase1-cpu.log | 第一阶段 62 项定向检查通过 |
| phase2-full-cpu.log | 第二阶段完整套件唯一一次运行，共 598 项 |
| full-test-classification.json、environment-retry.log | 24 个环境受限方法补跑通过；6 个既有失败方法保留 |
| review-fixes.log、review-fixes-final.log | 评审修改的定向检查及失败用例修正后的结果 |
| cache-gate-negative.log | 任意 rank 的实际缓存核对错误均使门禁失败 |
| mypy.log、static.log | 22 个源码文件类型检查、44 个 Python 文件静态检查通过 |
| old-entry-rg.txt | 生产与验证 Python 中旧算子入口零调用 |
| review.md | Standards / Spec 两轴评审结论和已修复问题 |

完整套件经环境补跑后共有 592 个方法通过，不能写成全套通过。
本机没有构建设备插件或运行双机 Graph、远端写读回、模型计算重叠。
运行方法和代码清单见 [交付说明](../../ticket26-delivery.md)。

完整套件日志归档时仅去除了 5 行末尾空白，测试结果和错误堆栈未改写。
