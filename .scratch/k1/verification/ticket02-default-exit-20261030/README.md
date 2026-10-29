# K1-ticket02 恢复默认退出的验证

用户2026-10-30明确要求沿用 SGLang 默认父进程退出方式，避免非必要主线改动。
本次基于 `bef317e497` 撤回退出扩展；全票净差异基点为 `993ae38ed6`。

- 完整 CPU：483 项通过，46.039 秒，`full-cpu.log`；本次仅跑一次完整套件。
- 严格 mypy：6 模块通过，`mypy.log`。
- 14 个修改 Python 文件 lint 与 Python 3.9 语法检查通过；恢复原样的文件未重新格式化。
- 双角色 `--describe` 通过：仍是两轮，第二轮为默认进程清理；`prefill-preview.json`、`decode-preview.json`。
- `scope.json` 记录逐字节恢复检查和本票最终代码范围。
- Standards：0项未解决发现；Spec：0项未解决阻塞。

本次删除7项主动退出相关测试，完整套件由490降为483项。
NUMA/池会话与映射、ready/free记账及既有错误判据保留。
公共scheduler、prefill、decode、MainRouter与服务启动shell恢复票前版本。
服务退出沿用已有进程清理路径，仍核对进程、端口与重启；不再证明逐rank主动BM关闭。

本次agent未执行NPU。NPU002/003历史失败记录保留，ticket保持open。

CPU入口：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```
