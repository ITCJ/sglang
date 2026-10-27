# Ticket04 tokenizer 返回值修复

用户在 `72fd610af8` 执行 `ticket04-001`，收到
`ValueError: template tokenization differs from saved input IDs`。
服务尚未启动，V1–V10 与故障、稳定性、清理项均为 not-run。

本地使用 [GLM-5.2 官方 tokenizer](https://huggingface.co/zai-org/GLM-5.2/tree/cf457fa734ab149ffef225f80893eb38c6ff5cdc)
和 Transformers 5.12.1 复现完全相同的异常。文件版本、SHA256 见 tokenizer-manifest.json。
完整 ABCE 与最小 128-token 输入均在修复前失败。

探针确认默认返回 BatchEncoding，list(result) 得到 input_ids/attention_mask 字段名。
result["input_ids"] 与 encode(rendered_text, add_special_tokens=False) 完全相等。
显式 return_dict=False 后返回 IDs 列表，模板渲染与编码无需改变。
这说明本地复现由 API 返回类型变化触发；用户机器版本未另行获取。

修复只增加一行参数，保留严格模板 IDs、精确长度和前缀验证。
真实 Transformers API 回归使用内存内轻量 byte tokenizer，无网络或权重依赖。
公开 GLM-5.2 全部六个输入也在本地通过，不代表模型答案或 NPU 验收通过。

复现命令：

```bash
python3 /private/tmp/mempool-ticket04/run.py tokenizer-red /private/tmp/mempool-ticket04-tokenizer-fix/repro.py
python3 /private/tmp/mempool-ticket04/run.py tokenizer-minimal-red /private/tmp/mempool-ticket04-tokenizer-fix/minimal.py
```

永久回归：

```bash
PYTHONPATH=ascend-mempool-test/src:ascend-mempool-test/scripts:ascend-mempool-test/tests/unit \
  python3 -m unittest test_mempool_full.TestLogicalFixtures -v
```

完整 470 项 CPU 回归通过（44.284 秒），静态检查通过，日志见 checks.json。
硬件重跑使用两机相同新版本和 run-id=ticket04-002；先 P 后 D，配置与模式不变。
功能源码阅读入口：ascend-mempool-test/scripts/mempool_logical_cases.py。
这是服务外测试输入生成职责，无主线或 NPU 服务行为改动。
