# Ticket26：关闭observe/trace的普通请求性能测量

用户报告024 trace超过100G、机器内存不足，明确取消trace和observe。
用户此前已取消phase1。本轮只做baseline与当前phase2两版，逐段人工确认清理。
024结果不记通过；旧进程/trace仍由用户按约定处理，不自动删除文件或重置设备。

## 范围与指标

两版同一4096-token输入、固定32-token输出，一个请求，greedy/ignore_eos。
observe=false、observe_trace=false、verify_prefix=false；不调用observation/flush，不启动设备profiler。
关闭verify_prefix只取消额外内容验收副本与校验，prefix匹配/存储策略保持配置原值。
P eager、D Graph保持不变。保留模型原生启动/Graph预热，额外HTTP warmup关闭。
新源码目录独立于原工作区，历史基点aa5852624e，当前版取准备时HEAD。

原生产Router的generate是非流式接口。仅验收脚本的MeasurementServer继承原MainServer，
复用原分配、dispatch、完成与cleanup；向D原生generate请求stream=true、stream_interval=1。
两个版本使用同一测量适配器，生产源码没有修改。标准逐token HTTP输出开销包含在测量中。

| 字段 | 口径 |
|---|---|
| router_ttft_ms | Router接单到收到D首token，P宿主单调时钟；不含客户端到Router网络与解析 |
| router_first_chunk_ms | Router接单到首个带token的D消息；若首块含多个token，TTFT为null |
| tpot_ms | 首块之后到最后token的时长／新增token数，普通逐token情况等于(last-first)/(N-1) |
| decode_tokens_per_second | 上述TPOT的倒数，按秒计算 |
| client_e2e_ms | 客户端发请求到收到完整响应，包括Router最终保存/释放与网络开销 |
| client_e2e_per_output_token_ms | 客户端E2E/N，不冒充decode TPOT |
| client_output_tokens_per_second | N/客户端E2E |

TPT有歧义，因此分别输出TPOT和E2E/N。标记first_token_exact，合并首块时不冒充精确TTFT。
仅一个请求，不报告稳定p95/p99，不包含设备级计时或计算重叠结论。
报告performance_measured表示测量完成，不等于整个ticket完整验收。
用户已取消三版本来源/trace补采；原证据缺口保留，不把普通延迟替代设备重叠证据。

## 026 baseline：准备（每侧一个指令）

先由用户确认024已结束、两侧资源可用。在31容器：

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
python3 ascend-mempool-test/scripts/prepare_migration_sample.py \
  --variant baseline \
  --root /home/cryang/log/k2/k2-ticket26-026-baseline-perf \
  --config /home/cryang/log/k2/k2-ticket26-017-target/config.json \
  --cases /home/cryang/log/k2/k2-ticket26-015-reference/prefill/cases.json
```

32同命令，将cases路径中的prefill改为decode。prepare输出observe/trace均false。
不使用024旧checkout；不需要重新编译multibase。不要删除015输入和017配置。

## 026运行

31先启动：

```bash
cd /home/cryang/log/k2/k2-ticket26-026-baseline-perf/source &&
python3 -u ascend-mempool-test/scripts/diagnose_target_startup.py \
  --role prefill \
  --config /home/cryang/log/k2/k2-ticket26-026-baseline-perf/config.json \
  --run-id k2-ticket26-026-baseline-perf \
  --sample-json /home/cryang/log/k2/k2-ticket26-026-baseline-perf/request.json \
  --without-index-sync --legacy-kernel
```

32同命令，仅role改为decode。每侧成功PERFORMANCE_MEASURED后退出，不自动开始当前版。
回传两侧summary.json与performance.json；原响应sample-response.json包含输出IDs供版本对照。
027当前版在确认清理后另行交付，不跑phase1。旧日志与旧配置不覆盖。

## 改动与验证

Ascend/NPU生产代码：无；通用主线生产代码：无。
验收脚本measure_mempool_router.py接收D原生SSE并计算同钟时间，不修改HTTP生产接口。
diagnose_target_startup.py的单请求分支改用测量适配器，取消trace导出；启动诊断默认分支不改。
prepare_migration_sample.py默认强制关闭observe/trace/verify_prefix，移除phase1选择。
测试test_migration_sample.py核对隔离版本、一次请求、关闭观测/导出与原清理。
测试test_mempool_performance.py核对token计时、重复结束元数据、合并首块、错误/截断流。
首轮7项CPU通过，ruff通过；baseline源码导入新runner与测量Router通过，未访问NPU。
第二轮8项CPU通过，补充适配器P非流式/D流式与原完成路径验证。
Standards/Spec各0项未解决；硬件性能测量等待用户执行。
