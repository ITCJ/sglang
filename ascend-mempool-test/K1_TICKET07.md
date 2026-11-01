# K1-ticket07：P alive Index K 单份

目标布局为 P 主 KV / D 主 KV / P Index K / prefix 主 KV / prefix Index K：
`8 / 8 / 1 / 16 / 16`。Index K 由 rank 0 在 NUMA 0 分配，默认只有 rank 0 写入。
全部 P 分片持有这份数据的设备映射，admit 各自写入仍然私有的 prefix。
底层 BM 会话包含 P/D 共 32 个参与者；物理副本分组与会话成员分别记录。
此版本仍用原来的索引拷贝；异步执行器在票 11 实现。

## 每机一条命令

复用前票配置。新环境在两机保存相同 JSON：

```json
{
  "prefill_host": "<P_IP>",
  "decode_host": "<D_IP>",
  "model_path": "/<LOCAL_GLM52_WEIGHTS>",
  "interface": "<NETWORK_INTERFACE>",
  "numa_nodes": [0, 1, 2, 3, 4, 5, 6, 7]
}
```

两机使用相同提交与模型。先启动 P，再启动 D，后续自动协调。
run-id 两机一致；失败后保留原目录并使用新 run-id。

```bash
# P 容器，仓库根目录
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode index-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket07-001
```

```bash
# D 容器，仓库根目录
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode index-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket07-001
```

`--describe` 仅打印有效配置、预算和阶段，不启动服务。
入口自动开启 P/D 主 KV 共享、Index K 单份、40 页 prefix、命中与既有源快照验证。
BM 本机贡献预算为 P 105 GiB、D 8 GiB，另计既有验收源快照预算。
BM store 使用 P 的 19000；NIC 保留 25670–26181，保持现有控制、HTTP 和传输端口。
仅清理本入口启动的进程组，沿用默认父进程退出，不新增逐 rank 主动关闭回执。

普通 `serve` 用配置启用：`prefill_shared`、`decode_shared`、`prefill_index_shared` 均为 true，
`prefix_pages` 为 40、`match_prefix` 为 true；`verify_prefix` 可关闭。
`prefill_index_writers` 默认 `[0]`，可选 `[0, 1]` 按连续整页分写；本票默认硬件验收只测 `[0]`。
P 的 rank 0 必须落在 NUMA 0；增加映射不会增加物理副本。

## 两轮功能验收与一组逻辑精度实验

复用既有两轮 target/restart，每轮 9 个请求，不增加第三次模型启动。
两轮均用于功能验证，不产出性能统计。

1. 运行原有 A/B/C/E、长生成和提前结束 6 个请求。
2. 追加同一组 3 题，依次为冷请求、复用 A 并改题、完全命中 A。
3. 首轮在请求完成后注入超时，检查 fail-stop 和双端清理。
4. 第二轮复用端口重启，重复请求并检查正常释放。

| 新增题 | prompt / 命中 token | 独立答案 |
|---|---|---|
| index-cold | 4096 / 0 | 31 块砖送出 8 块再收到 6 块，剩 29；奇数选 west |
| index-update | 5120 / 4096 | A 中 22 支铅笔送出 7 支再收到 2 支，剩 17；蓝色选 north |
| index-full-hit | 4096 / 3968 | 原 A 的 harbor、22、north |

模板按完整句子填充；核对实际 token 前缀和命中边界。
使用独立 JSON 答案判定，A 与最后一题的输出 token 差异单独记录。
任一逻辑答案或命中边界错误即返回非零，保留输入、输出、答案和运行日志。

2026-11-01 用户明确要求：不增加直接读取 BM 数据的验证。
逻辑实验通过时，继续使用计算源快照、正常 load_back 后的 native HBM 与写入计数。
出现精度问题时先分析失败证据，再讨论直接读 BM 或其他替代方案；脚本不自动开启读回。
已有 P 主 KV 启动哨兵沿用前票，未新增请求级 BM 读回。

## 检查与覆盖边界

- 注册、实际贡献量和区域 owner 一致；P Index K 仅一份、owner 0、NUMA 0，16 个 P 映射者。
- 实际 Index K 写回和 fill 行数符合写者分工；默认其他 15 个 rank 均为零。
- 就绪等待 Index K 的写回与 fill；admit 和源释放仍等待全部参与分片。
- CPU 测试核对共享源、各私有 prefix 的拷贝内容及可选双写者。
- 实机内容证据来自既有源快照与正常恢复数据，不声称逐份直接读取 prefix Index K。
- V3 沿用独立逻辑答案，V8 重放真实注册；物理 NUMA 驻留单列，权限不足保留 unverified。
- 只做功能验收；性能工具仍留专门测试分支，完整组合验收归票 09。

日志保存到 `/home/cryang/log/mempool-min/k1-ticket07-001/{prefill,decode}/`。
回传两机终端结果、P 的 `summary.json` 和 `index-sharing-acceptance.json`。
各轮还包含 `index-accuracy.json`、`responses.json`、`answers.json`、`registrations.json`、
`snapshots.json`、`evidence.json` 和清理结果；失败日志一并保留。
自动报告保留 `user_accepted: false`，用户确认后才关闭 07 并解锁 08。
# 002失败后的定向冷对照

本次只诊断保存的 `index-update`：共享布局仍为8/8/1/16/16，关闭前缀匹配，
原样发送保存的 `input_ids` 与采样参数。每台1轮服务、1条请求，自动检查就绪并清理。
不重跑fetch/九例/故障注入，不新增BM读回，不修改提示词或答案标准。
这是同一mempool布局的无命中对照，不是无mempool正式服务基线或完整验收。

两台更新到包含 `index-cold-diagnosis` 的相同版本，在仓库根目录先31后32运行。
31保留002的原始responses文件；32不需要复制该文件。

31：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode index-cold-diagnosis \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket07-cold-001 \
  --replay-responses /home/cryang/log/mempool-min/k1-ticket07-002/prefill/target/responses.json
```

32：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode index-cold-diagnosis \
  --config /home/cryang/mempool-min-ticket01-3132.json \
  --run-id k1-ticket07-cold-001
```

返回31的 `/home/cryang/log/mempool-min/k1-ticket07-cold-001/prefill/target/index-cold-diagnosis.json`
和两台终端结果。若仍答错，双机清理后返回非零属诊断结果，不代表服务崩溃。
原始响应、正常解码文本、状态快照及D日志均保留在target目录。
结果相同说明问题不依赖本次前缀命中，但仍不能排除共享路径本身；结果不同则继续定位复用差异。
