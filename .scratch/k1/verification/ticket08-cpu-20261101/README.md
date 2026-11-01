# Ticket08 本机验证

基点 `fd1541b8d9`。NPU 未运行；本机检查不关闭 ticket08。

- 完整 CPU 套件运行 532 项，32.031 秒，无断言失败；21 个测试方法因沙箱禁止本机 TCP/`ps` 产生 25 个错误记录。见 [原始日志](full-cpu.log)。
- 只在沙箱外补跑这 21 个方法，22.617 秒，全部通过。见 [补跑日志](retry-cpu.log)。合并覆盖 532 项通过，没有重跑完整套件。
- 补跑选择器最初把方法名拼接了两次，没有执行实际测试；错误日志单独保存在 [retry-selection-error.log](retry-selection-error.log)，随后只修正选择器。
- [静态检查](static.log)：24 个修改 Python 文件通过 ruff、格式、isort、Python 3.9 AST；18 个功能模块（含类型依赖）通过 strict mypy。
- 定向红绿覆盖池分段/stride/探针、交错保存、单写者门控、1/31/32 页恢复、八份 fill、真实源摘要、双侧 CLI 和分段注册。
- 全套之后仅在代码修复或审核发现需要时补受影响集合；不重复模型初始化，不联系 NPU。

完整套件命令：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  /private/tmp/k1-ticket05-fix-venv/bin/python -m unittest discover \
  -s ascend-mempool-test/tests/unit -v
```

测试将不同源组和写者设为不同值，通过实际映射拷贝和原生 HBM 缓冲检查内容。
Main/SR 的控制与额度在各自真实接口检查；本机未启动完整模型或真实 HCCL/BM。
用户硬件验收入口及缺项见 [K1_TICKET08.md](../../../../ascend-mempool-test/K1_TICKET08.md)。

[Standards/Spec 双轴评审](review.md)均0项发现。

[Planner CLI预览](planner-preview.json)验证八段各5页、P 96 GiB及零贡献follower；没有分配BM。
25个交付文档本地链接有效，两份HTML内嵌源稿与Markdown一致。
原始日志保留unittest输出的尾空格；代码和文档的diff空白检查通过。
