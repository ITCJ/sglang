# K1-ticket02-002：P 正常关闭超时

用户在 31 回传 `session-acceptance` 的终端输出，run-id 为 `k1-ticket02-002`，
配置为 `/home/cryang/mempool-min-ticket01-3132.json`。
两轮服务均已启动，第二轮最终报：

```text
TimeoutError: prefill graceful shutdown exceeded 60s; see /home/cryang/log/mempool-min/k1-ticket02-002/prefill/restart
```

上一交付提交为 `d10e01cc9b`；本次实际运行 SHA 尚未回传。
agent 未访问 NPU，尚未拿到两侧服务日志与完整报告。

## 已核对的等待边界

`Runner.close_session()` 先等待 D 主服务进程以 0 退出，跨机 barrier 后才向 P 发 SIGTERM。
错误来自 P 主服务进程的退出等待；该错误本身不能证明 BM leave/uninitialize 阻塞。
Tokenizer 的正常关闭还包含请求排空、ShutdownReq、scheduler 资源释放和子进程回收。
需要两侧 `restart/prefill.log`、`restart/decode.log` 中相应阶段的证据。

此前 CPU 子进程测试覆盖 D→P 顺序，池替身测试覆盖 drain/leave/destroy；
它们没有复现这次真实服务关闭超时。当前没有能复现该 NPU 症状的本机反馈环。
按 diagnosing-bugs 的 Phase 1，先索取现有日志，再决定复现接口；不据超时信息修改释放逻辑。

本轮验收失败，票据保持 open。正常关闭、各 rank 的 session_closed 和最终汇总均待确认。

## 用户补充日志与本机复现

用户随后提供两侧 restart 日志，要求“先分析问题”。本轮未修改功能代码。

- D：21:13:59 开始关闭 BM，21:14:01 的 16 个 rank 均报告 `session_closed`。
  BM uninitialize 用时约 2.48–2.59 秒。
- D：21:14:14，Tokenizer 报告全部 16 个 scheduler 在 ShutdownReq 后 15 秒仍存活，
  调用 kill_process_tree；21:14:15 主服务退出。
- P：21:14:14，SR 报 `Host unreachable`，scheduler 抛出 fail-stop，触发 SIGQUIT。
  随后出现多条 `double free or corruption (fasttop)`。
- P：21:14:25 明确进入 CUDA coredump 的 60 秒等待，21:15:25 才调用 kill_process_tree。

实际 MainServer.pump、真实本机 TCP/ZMQ 和实际 SR 代码已复现传播链：
空闲 P/D 注册完成后，仅关闭 D 的 DEALER socket，Main 的 ROUTER_MANDATORY 心跳发送
报 Host unreachable，Main 随后将 error 发给 P，P 的 16 个 SR 全部 fail-stop。
临时诊断脚本 `/private/tmp/k1-ticket02-shutdown-repro.py`，约 2.032 秒：

```text
Before D exit: both registered, no requests, Main healthy
mempool_router fail-stop: Host unreachable
After D exit: {"run_id": "cpu", "role": "prefill", "kind": "error", "reason": "Host unreachable"}
SR fail-stop: Host unreachable
P ranks faulted: 16
AssertionError: Normal D shutdown faulted the still-running P through Main
```

运行命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python /private/tmp/k1-ticket02-shutdown-repro.py
```

确认的缺口：Runner 等 D 整个主进程退出后才开始关闭 P，而 Main 一直保持正常工作态，
没有受控停机/退注册过程。因而 D 的预期断开触发仍在运行的 P 故障。
该复现不依赖 NPU，也不需要先出现 double free；日志中的 60 秒崩溃诊断等待解释了外层超时。

另两个现象仍未定位：D 在 BM 关闭完成后的其他清理或进程终结为何超过 15 秒；
P 的 double free 具体来自哪个 native 释放路径。BM 关闭回执不代表整个 scheduler 已退出。
本机复现不声称重现了这两个 native 问题。

建议方向：所有请求释放后，先让 Main 和两侧 SR 进入受控停机，再执行 D→P 的 BM 关闭。
保留工作期间的断连 fail-stop；不能把一般 Host unreachable 忽略，也不能用调大超时替代修复。
用户本轮仅要求分析，尚未实施修复或重新执行 NPU。

## 后续：用户授权实现受控停机

用户随后要求开始修改。MR/SR停机确认已接入，控制面确认完成后才允许D→P退出。
真实TCP/ZMQ、子进程编排和报告回归通过；完整490项CPU、6模块strict mypy及两轴复审通过。
正常关闭报告增加全rank后续通信清理正向回执，并拒绝强杀/崩溃。
native滞留与double free仍未确认解决；保留原始失败结论，等待NPU003。
