# K1-ticket02 Mac 验证

基点：`993ae38ed6`。以下均为agent在Mac实际运行；NPU未执行。

- 审查前完整CPU：483项通过，45.106秒，`full-cpu.log`。
- 审查修复后最终完整CPU：485项通过，45.494秒，`final-cpu.log`。
- 入口与原完整报告定向回归：35项通过，`entry-regression.log`。
- 新精简入口定向：5项通过，含本机TIME_WAIT与D→P实际子进程关闭，`session-final.log`。
- 10模块strict mypy通过，`mypy.log`。
- 21个Python文件lint、format、Python 3.9语法检查，以及两个shell入口语法通过。
- 双角色 `session-acceptance --describe` 通过，`prefill-preview.json` / `decode-preview.json`。
- [Standards / Spec 两轴审查](review.md)最终均无剩余发现。

全套入口：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/sglang-pr43579-review-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

开发Mac的旧 `sglang-merge-venv` 缺torch，改用已安装依赖的上述环境。
最初沙箱禁止ps/进程组清理，放行本机CPU测试所需操作后通过；不涉及远端NPU访问。
TDD确认的边界为池会话、SR对外ready/free与注册、验收入口。
新增断言先在缺少相应功能时失败，再补最小实现；原接口测试迁移到真实会话路径。

硬件命令与覆盖边界见[运行说明](../../../../ascend-mempool-test/K1_TICKET02.md)。
本地通过不代表模型内容、Graph、实际BM关闭或NPU重启已验收。
