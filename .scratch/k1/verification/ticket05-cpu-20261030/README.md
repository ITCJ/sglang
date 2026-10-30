# K1-ticket05 本机验证

基点：`4c0e79b55046ba69a1559eaab39d061ce3eec8ed`。
本次覆盖 502 个 CPU 用例，最终全部通过；未运行 NPU。

## 执行次数与实际结果

六个新增接口用例先失败后通过，已各完成红绿两轮。
最终回归保留这六项已有的通过结果，执行其余 495 项，避免第三次重复。
没有减少用例覆盖；不把此前通过结果冒充最后一次命令重新执行。

495 项中，21 项因沙箱禁止本机 socket 或 `ps` 报 PermissionError。
另有一项旧 worker fixture 缺少新的 `contribution` 字段，已补齐 fixture。
只在沙箱外补跑这 22 项，全部通过，没有重跑整个套件。

证据：[首次回归](full-cpu.log)、[22项补跑](retry-cpu.log)、[静态检查](static.log)。
首次报告的错误保留，未改写为单轮全通过。
一个新增证据测试曾使用错误的默认 prompt 容量；改为票据规定的8192后，在最终回归通过。

## 六个红绿接口用例

- 池会话：共享 P 主 KV，私有 Index K、prefix 和 D 主 KV。
- runtime：按页分工写入，允许空的本 rank 后缀写入。
- prefix：分工 fill 主 KV，完整 fill 私有 Index K。
- SR：先写后半页不能提前 ready，同组共享页描述符。
- Main：接受 P 8 份、D 16 份注册。
- 验收入口：两轮模型启动及 120 GiB P BM 贡献预算。

另有组合用例覆盖两种写策略的主 KV、Index K、admit、fill；
延迟最后写者清理用例验证槽不提前归还；证据反例拒绝附属区域误共享或缺少 peer 读取。

## 检查命令与文件

CPU 使用 `/private/tmp/sglang-pr43579-review-venv/bin/python`。
`PYTHONPATH` 包含 `ascend-mempool-test/src`、`scripts`、`tests/unit`。
最终回归用 unittest discover 加上述六项过滤；过滤清单保存在首次日志头部。
补跑按首次日志中的不同测试 ID 加载，只包含失败的22项。

功能源码通过 strict mypy，修改 Python 文件通过 ruff、格式、isort 和 Python 3.9 语法检查。
最后的文件数和命令以 `static.log` 为准。

测试文件：`test_pool_session.py`、`test_runtime.py`、`test_prefix.py`、
`test_mempool_subrouter.py`、`test_mempool_router.py`、`test_mempool_session_acceptance.py`、
`test_shared_prefill.py`、`test_mempool_shared_evidence.py`、`test_mempool_min_entry.py`。
它们均位于 `ascend-mempool-test/tests/unit/`。

## 评审与硬件边界

Standards 初审0项硬性违规、1项命名建议；Spec 初审发现1项验收入口路由问题。
两项均已修正，独立复审两轴均为0项未解决问题。
详见[双轴评审](review.md)。

评审后新增第502项回归：启动阶段按当前配置选择共享注册校验。
该项先因缺少config参数失败，修复后通过，并验证错误副本数仍被拒绝。
没有重跑此前已通过的501项；此回归自身只有红绿两轮。
共享报告将未检查的旧计数器等价事件数记为null，另报实际ready事件数。
CPU 的 BM/NPU kernel/event 替身不能证明真实共享映射、设备可见性或 Graph replay。
真实硬件入口、命令和证据边界见 [K1_TICKET05](../../../../ascend-mempool-test/K1_TICKET05.md)。
State 保持 open，等待用户 NPU 验收与关闭确认。
