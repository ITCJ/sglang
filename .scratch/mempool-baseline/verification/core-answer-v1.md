# core-answer-v1 本地验证

用户授权：接受核心答案正确、强制128-token续写不同；完整IDs不一致仅记录，
不停止实验、不跳过C/E，5轮ABCE、首轮预热、轮间整套重启保持。
逻辑和其他检查失败记录，全部跑完统一分析，用户决定是否验收。

## 结果

- CPU unittest discovery：475 tests，OK，47.632s。
- 最终相关pytest（perf报告/双端轮次 + 共享入口）：39 passed、8 subtests passed，10.59s。
- 6个Python文件通过ruff、isort及Python3.9语法检查；git diff --check通过。
- P/D的perf及full-acceptance共4个describe入口通过；普通功能验收策略保持原值。
- 未执行本次修改后的NPU测试；不将上述结果视为真实基线或用户验收。

相关测试命令（使用本地review venv与既有脚本PYTHONPATH）：

```bash
python -m unittest discover -s ascend-mempool-test/tests/unit -q
python -m pytest ascend-mempool-test/tests/unit/test_mempool_perf.py \
  ascend-mempool-test/tests/unit/test_mempool_min_entry.py -q
```

全量unittest完成后新增了参考B长度异常仍继续C/E的入口回归；该新增用例包含在最终39项中。

## 关键证据

真实本地Peer和自有CPU子进程替代NPU/HTTP边界，完整执行5轮20条请求。
每轮固定ABCE，15个唯一子进程（每轮Main、P、D）均退出，双端outcome一致。
第1轮B为127-token错误，第2轮B仅完整IDs从索引30起不同，第3轮B核心答案错误，
第4轮B账本错误，第5轮正常；每轮都发出4条请求，错误B没有使C/E作废。

离线完整归档测试验证：仅IDs不同仍获得每指标4个有效测量请求；完整参考/实际输出、
逻辑解析、精确续写、首差、含预热的跨轮分组可从output-analysis.json读取。
参考逻辑错误、目标逻辑错误、缺设备事件、长度和账本错误仍标无效并保留可提取计时。
解析器拒绝重复字段、数值类型错误、非开头JSON和缺字段，不从错误答案之后找正确答案。
旧manifest未声明新策略时保留严格比较结果；用户验收字段始终false。

真实NPU下一次运行使用新run-id baseline01-002，避免覆盖baseline01-001。
