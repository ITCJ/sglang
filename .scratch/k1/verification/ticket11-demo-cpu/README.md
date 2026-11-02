# Ticket11 demo 本地验证

基点abdee9d701。NPU未运行，11保持open。

- TDD：系统python缺torch的首次尝试为环境失败；切到既有临时venv后确认模块缺失的red。
- 完整CPU一次547项（unittest.log）。新增执行器状态与BM wait未知测试通过。
- 25个沙箱错误包含子用例，来自21个测试方法；仅这些方法以允许本地socket/子进程的权限补跑，21项全部通过（retry.log）。
- 合并结果546/547通过。剩余test_config.test_defaults_and_unsupported_model_storage仍期待UINT32_MAX错误，与已有page-first分段寻址不一致。本次未改该文件或配置/布局代码，不计通过，不额外重跑。
- ruff（独立测试项目配置）、AST、pool_copy strict mypy通过；CLI --help通过。未进行硬件模拟替代NPU验收。
- 不对同一测试进行第三轮；没有重跑旧模型或服务。

## Standards

初审发现最终JSON非原子发布、wait未知状态分类。已修复并只读复核，无剩余阻断。
worker函数偏长为非阻塞建议；按demo简单优先保留，未引入框架。

## Spec

初审发现初始化异步源未等就绪、wait分类、接口报告不足和失败部分证据缺失。
均已修复并只读复核，无剩余阻断。GIL与可见性契约、子提交错误传播仍是未验证事实，不算硬件通过。

本次实际功能源码为NPU pool_copy.py及manager.py的装配方法；通用主线功能源码0。
测试与运行说明见ascend-mempool-test/K1_TICKET11_DEMO.md。
