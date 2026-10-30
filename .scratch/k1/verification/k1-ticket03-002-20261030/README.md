# K1-ticket03 NPU002：就绪探测失败

用户在31侧以`split-read-acceptance`、`k1-ticket03-002`运行。
配置沿用`/home/cryang/mempool-min-ticket01-3132.json`。
交付版本为`e6b8f6f487`；用户未回传本轮Git身份文件，实机SHA尚未核对。

## 用户回传证据

`prefill/summary.json`：

```json
{"status": "failed", "error": "BadStatusLine: GET /ready HTTP/1.1\r\n"}
```

两个effective-config的host/port一致：P=10.120.72.31:8000，D=10.120.72.32:8001，
Main=6699，control=16700，runner=18891，bootstrap=8995，未见配置端口重叠。
`target/router.log`为空。launcher.log与prefill.log尾部相同：
分片加载到96/96，最后一条为12:50:30 TP15的`rot.weight not found in params_dict.`。
此前多个rank记录`ASCEND_OPP_PATH`，所给尾部未见服务HTTP就绪。
这些事实不能证明上述权重提示是故障原因，也不能确认具体失败的探测URL。
没有本轮KV恢复、精度或计时通过证据，ticket保持open。

## 复现与修复范围

把用户回传的首行作为socket响应，执行真实`wait_http`及urllib HTTP解析器。
旧版本稳定抛出相同BadStatusLine，三项回归测试均失败。
调查启动代码、辅助端口与探测代理配置，未找到回显来源；代理在探测中已禁用。
本机TCP自连接试验被macOS以EINVAL拒绝，不能把TCP自连接认定为远端根因。

确认的脚本问题是：原重试分支仅捕获OSError/URLError，BadStatusLine会退出runner，
并由既有Processes清理仍在初始化的服务。现将BadStatusLine纳入原有有界重试。
仍仅HTTP 200成功，仍调用进程/对端检查；超时报告URL和最后异常。
若非HTTP响应持续到超时，验收仍失败，需要继续核对远端来源。

本次功能源码净改动：Ascend/NPU=0，通用主线=0。
脚本：`ascend-mempool-test/scripts/run_ticket10.py`，修复共享wait_http。
测试：`ascend-mempool-test/tests/unit/test_acceptance_runner.py`，新增三个字节回放用例。
说明：本记录、票03 Comments、K1_TICKET03运行指令与target-alignment D节。
不修改baselinetest；已推送的ticket03通过后续修复commit交付，不重写远端历史。

## Mac检查

定向命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest \
  test_acceptance_runner.TestHTTPReadiness -v
```

修复前3项BadStatusLine错误；修复后3项通过（0.017秒）。
覆盖回显→503→200、持续回显超时、回显后进程失败立即中断。
此测试保留真实HTTP解析器，只替换socket字节来源和等待时间。
run_ticket10.py的strict mypy通过；两个Python文件ruff/isort/格式检查通过。
完整CPU仅运行一轮，494项通过（46.669秒），原始输出见`full-cpu.log`。

## Standards

0项硬标准违规，0项需处理异味。改动限于既有验收脚本，保留默认清理；
三个独立行为的测试保留真实HTTP解析器，文档如实区分回放验证与NPU待验。

## Spec

0项发现。沿用有界等待及HTTP 200成功条件，保留进程/对端失败传播；
记录不把尚未证实的远端回显来源写为根因，NPU002失败及ticket open均保留。
两轴由独立agent只读复审，未额外运行测试。

NPU003命令见[运行说明](../../../../ascend-mempool-test/K1_TICKET03.md)。
真实NPU启动及split_read验收仍待用户运行；本地通过不代表硬件验收通过。
