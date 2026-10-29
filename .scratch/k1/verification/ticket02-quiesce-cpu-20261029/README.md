# K1-ticket02 受控停机修复验证

基点 `d10e01cc9b`。agent 在 Mac 执行；本次 NPU 修正待用户运行 `k1-ticket02-003`。

- 完整 CPU：490 项通过，48.227 秒，`full-cpu.log`；本次完整套件运行一次。
- 收尾保留原有双方注册齐全后才计心跳超时的语义，18项MR定向回归通过，`router-final.log`；未重复完整套件。
- 严格 mypy：6 个修改模块通过，`mypy.log`。
- 12 个修改/新增 Python 文件 lint、format、Python 3.9 语法检查通过。
- Standards：最终无未解决发现；将测试 fixture 改为模块导入，避免重复收集已有10项测试。
- Spec：最终无未解决阻塞；按审查补齐双方全部rank的通信清理完成回执，避免BM之后的finally异常被主进程零退出掩盖。

## Red / green 与证据范围

1. MR 在单侧 free_ack 后停止接单，但必须等待另一侧释放才发 quiesce；缺少最后一个 rank 的回执不能完成停机。
2. SR 使用真实 TP 共识确认空闲后停止协议处理；真实 service 生命周期覆盖旧 P/D 控制 tick 同时停止。
3. 实际 Main/SR 加真实 TCP/ZMQ：受控 D 断连原先报 `Host unreachable`，修复后通过；未声明的断连仍 fail-stop。新增模块只收集2项。
4. 实际子进程：未完成 MR/SR 确认就收到终止信号会失败；确认完成后按 D→P 退出通过。
5. 报告：只有 BM 回执、缺少后续 worker cleanup 的日志必须失败；包含已知强杀/崩溃的日志同样失败。

## 运行入口

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

mypy 使用 `--strict --follow-imports=silent --ignore-missing-imports`，检查
`mempool_router.py`、`mempool_router_transport.py`、`mempool_subrouter.py`、
`mempool_service.py`、`run_mempool_min.py`、`verify_mempool_session.py`。

真实 socket / 自建子进程测试需要本机沙箱放行；未访问远端 NPU。
D 在 BM 关闭后的滞留及 native double free 未在 Mac 复现，不能据这些结果宣称已解决。
本轮用结构化退出阶段回执和失败判据保留问题可见性。
