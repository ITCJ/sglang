# K1-ticket02：share=0 适配器与精简验收

保持已验收的 GLM-5.2 环境：P、D 各一台 TP16，page 128，P slot 8192、D slot 128。
使用同一代码版本、权重和依赖，CANN / ATB / MemFabric 环境沿用 ticket01。
默认 NUMA 0–7 连续分配，保持 share=0。四类副本仍各 16 份，D 只持有 alive 主 KV。

## 每机一条命令

可以复用 ticket01 的 `/home/cryang/mempool-min-ticket01-3132.json`。
新环境的配置如下，将占位符替换为实际值；两机保存相同配置。

```json
{
  "prefill_host": "<P_IP>",
  "decode_host": "<D_IP>",
  "model_path": "/<LOCAL_GLM52_WEIGHTS>",
  "interface": "<NETWORK_INTERFACE>",
  "numa_nodes": [0, 1, 2, 3, 4, 5, 6, 7]
}
```

先 P 后 D，均在仓库根目录执行。P 会等 D，后续自动协调。
两边 run-id 相同且未使用；失败后使用新 run-id，保留旧日志。

```bash
# P 容器
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode session-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket02-004
```

```bash
# D 容器
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode session-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket02-004
```

加 `--describe` 可在启动前预览实际配置、两轮阶段和容量。
依赖与进程检查、服务 readiness、输入生成、请求、结果核对、日志收集和清理自动完成。
脚本只清理自己创建的进程组，失败返回非零；不会自动重跑整个验收。

## 两轮分别做什么

只运行一次 P-only / D-only / mixed 复制 smoke：block_dim 48、一次 eager、一次 replay、一次 warmup。

1. 首轮启动真实服务，执行 A/B/C/E、128-token 长生成和提前 EOS。
   核对 KV 内容、Graph、prefix 保存和命中、slot 复用、注册以及 ready/free 记账。
   随后暂停 P，验证超时 fail-stop、D 非零退出和双端进程清理。
2. 使用相同端口重启真实服务，再跑同组请求。
   请求全部 free_ack 后，使用原有父进程/进程组清理路径结束服务。
   检查双端进程清理和端口释放；不要求 worker 自行返回或逐 rank 主动 BM 关闭回执。

输入与独立预期答案沿用阶段0：A 4096 token；B 5120、命中4096；C 4096、命中3968；E 冷4096。
逻辑题按 JSON 标准答案核对，长生成只检查负载和实际 decode forwards。
ready 位图与原计数器在同一事件上逐条相等，每请求最终到完整 prompt。
全部副本的读者计数归零，才计入 free 证据。

## 精简范围与结果

该模式是 **K1-02 精简功能验收**。报告不冒充原 `full-acceptance` 全覆盖。

- 取消本轮参考服务加载；V3 使用独立题目答案，不重做参考模型 IDs 对照和冷 A/C 稳定性检查。
- 取消独立容量故障服务；V8 使用本轮真实注册，在 CPU 上重放 Main 的容量拒绝和账本不变检查。
- 将超时故障合并到首轮末尾；第二轮直接证明故障后可重启。
- 不重复 ticket01 的双映射完整验收；不运行性能基线。
- 故障后共享区域按进程退出回收，并以成功重启验证可用性。没有退出后的物理 DRAM 页计数证据。
- 按用户约定沿用 SGLang 默认退出，父进程终止 worker 属于正常清理；不增加第三次模型启动。

相较上次 `numa-acceptance` 的两种映射 × 四次服务启动，本票只启动两次模型服务。
不承诺具体分钟数，实际耗时待 NPU 运行记录。

两机日志在 `/home/cryang/log/mempool-min/k1-ticket02-004/`。
请回传 P 的 `summary.json`、`session-acceptance.json`，以及两机终端结果。
失败时保留相应阶段日志。关键证据在 `target/` 与 `restart/`：
`registrations.json`、`responses.json`、`answers.json`、`snapshots.json`、
`evidence.json`、`capacity-replay.json`、`ports-released.json`、`process-cleanup.json`。
`target/timeout.json` 记录故障；双侧服务日志保留在相应阶段目录。

按用户最新决定，已撤掉公共scheduler/PD循环的退出修改、quiesce及主动BM关闭回执要求。
历史NPU002/003的失败记录不变；本票仍待用户确认当前版本的功能与默认进程清理结果。

所有必要检查通过后脚本返回0。`user_accepted` 保持 false，用户确认本轮 NPU 结果后才关闭票据。
