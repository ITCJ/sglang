# K1 ticket09：四组完整功能验收

实现四组 `(B,S_D)=(128,128)/(128,512)/(64,128)/(64,512)`。
当前仅CPU验证与代码交付，四组NPU均未运行；09保持open。

## 运行

沿用31/32已确认的模型、驱动、CANN及ticket11修复SDK。
两侧使用相同提交、配置和SDK构建标识。P先启动，D随后启动；同一run-id。
原配置中的主机、模型、网卡、端口沿用，脚本自动生成四组配置。
B=64使用80页prefix，保持5120 tokens；S_D=512自动扩展context余量。
每组均开启8/8/1/1/1、按段读取、page first与pool_copy。

31/P，仓库根目录：

```bash
python3 ascend-mempool-test/scripts/run_k1_full_acceptance.py --role prefill --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket09-001 --sdk-build-id mf114-c01f3ad-ticket11-batchfix
```

32/D：

```bash
python3 ascend-mempool-test/scripts/run_k1_full_acceptance.py --role decode --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket09-001 --sdk-build-id mf114-c01f3ad-ticket11-batchfix
```

加 `--describe` 只打印四组有效配置，不启动服务。
每组运行一次reference、target、capacity、timeout，共16次成对服务启动。
ABCE及提前EOS输出上限128，长生成独立到S_D，保持单请求串行。
完整覆盖独立答案与冷A/C稳定性、真实容量故障、四类入口拒绝、超时和双端端口清理。
D验收开关 `verify_decode=true` 恢复逐步哨兵、末行比较和Graph延迟写者正负测试。
默认生产配置该开关false。退出沿用SGLang父进程路径。

## 回传与判据

日志位于 `/home/cryang/log/mempool-min/k1-ticket09-001/`。
两侧顶层报告为 `matrix-prefill.json`、`matrix-decode.json`。
子目录名包含顶层run-id和组合，例如 `k1-ticket09-001-b64-sd512/prefill/`。
请回传两份matrix报告及完整目录，保留effective-config、identity、topology、platform版本、
V1–V10、K1逐步/末行/跨页证据、原始请求响应和清理报告。

两侧退出0、四组各自完整报告passed才算自动验收通过；再由用户确认关闭。
失败非零，后续组合not-run；重测换run-id，最多两轮，不自动循环重试。
SDK构建字符串是审计标签，不单独证明部署二进制含补丁。

## 证据边界

- B=64完整模型/Index K/Graph能力待本轮NPU证明，CPU和配置放行不能代替。
- D每步检查上一token，结束后独立补查末行；512输出通常只有511次D forward。
- 受控BM组件通过生产SparseKVCopy读取每个页边界及最后两行，检查换槽Graph。
  它与真实模型选择及哨兵分别报告，不把受控读取冒充模型自然选择。
- P alive/prefix/fill没有新增目标全量读回，沿用源摘要、恢复HBM及覆盖/字节证据。
- 50ms为pool_copy提交前延迟；服务超时不等于真实SDK在途故障/超时。
- NUMA物理驻留不可观测时保留unverified；平台版本文件不可读时保留unavailable。
- 性能基线及三项遗留指标仍在baselinetest另行安排；此处不是性能验收。
- 当前CPU保留既有test_config的UINT32_MAX旧预期失败，详见CPU记录。

## 功能源码阅读顺序

前缀 `python/sglang/srt/`，全部为Ascend/NPU专用：

1. `disaggregation/ascend/mempool_router_protocol.py`：四组配置、prefix容量与默认关闭的D验收开关。
2. `hardware_backend/npu/mempool/decode_write.py`：逐步与末行D比较，读回使用page-first indexed_copy。
3. `hardware_backend/npu/mempool/decode_probe.py`：复用历史Graph顺序正负探针，适配当前会话/布局。
4. `hardware_backend/npu/mempool/runtime.py`：开关装配、事件完成后的统计与错误、末行检查。
5. `disaggregation/ascend/mempool_router_transport.py`：注册阶段的D顺序探针接线。

通用主线功能源码净改动为0。

脚本与测试另列：`run_k1_full_acceptance.py`编排矩阵；`run_mempool_min.py`复用四阶段；
`mempool_logical_cases.py`参数化题目；`verify_mempool_prefix/reuse/min/full/decode.py`核验内容与完整证据；
`run_pool_copy_acceptance.py`复用搬运检查；`verify_graph.py`传入B；`verify_fetch.py`强制跨页读取。
相关unit覆盖配置/扣账、逻辑题、报告拒绝、共享D数据和独立地址oracle。
