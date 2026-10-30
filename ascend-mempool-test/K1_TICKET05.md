# K1-ticket05：P alive 主 KV 共享

P、D 各一台 TP16，模型 GLM-5.2，page 128，P slot 8192，D slot 128。
P 主 KV 为 8 份；D 主 KV、P Index K、prefix 主 KV、prefix Index K 各 16 份。
两机使用相同提交、模型和配置，CANN / ATB / MemFabric 环境沿用前票。

## 每机一条命令

可以复用 `/home/cryang/mempool-min-ticket01-3132.json`。
新环境使用以下配置，两机保存相同内容：

```json
{
  "prefill_host": "<P_IP>",
  "decode_host": "<D_IP>",
  "model_path": "/<LOCAL_GLM52_WEIGHTS>",
  "interface": "<NETWORK_INTERFACE>",
  "numa_nodes": [0, 1, 2, 3, 4, 5, 6, 7]
}
```

先启动 P，再启动 D；入口自动协调后续操作。
run-id 两机一致，失败后保留旧目录并使用新 run-id。

```bash
# P 容器，在仓库根目录执行
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode p-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket05-001
```

```bash
# D 容器，在仓库根目录执行
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode p-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket05-001
```

增加 `--describe` 可预览有效配置、容量和执行阶段。
验收模式自动启用 `prefill_shared`、40 页 prefix、prefix 命中和内容验证。
默认分组 `{0,1}`、`{2,3}`…`{14,15}`，各组首成员为 owner。

生产 `serve` 模式通过配置中的 `"prefill_shared": true` 启用。
`prefill_groups` 可指定成员顺序，首成员即 owner；必须覆盖全部 TP rank，且同组同 NUMA。
`prefill_write_assignment` 支持 `contiguous`（默认）和 `round_robin`。
它控制主 KV 写入分工，与已存在的 `prefix_read_assignment` 分开。

## 自动执行的两轮

入口完成双端版本、配置、依赖和容量检查，以及已有的复制 smoke。
默认模型布局的 BM 本机贡献量为 P 120 GiB、D 16 GiB。
P 验收 CPU 源快照等额外预算为 32 GiB，入口一并检查。
这些数字不包含模型、原生 HBM 或驱动的虚拟地址预留。

1. 启动服务，等待 Main/SR 就绪，执行 A/B/C/E、长生成和提前 EOS。
   A 为冷 4096；B 为 5120、命中 4096；C 为 4096、命中 3968；E 为另一冷请求。
   随后暂停 P，验证超时 fail-stop、D 非零退出及双端进程清理。
2. 使用同端口重启，重复功能请求，检查释放复用。
   所有 free_ack 完成后，沿用 SGLang 默认父进程清理，核对端口释放。

只清理本入口创建的进程组；失败返回非零，不自动增加第三轮。
不增加逐 rank 主动 BM 关闭回执，也不改变通用 shutdown。

## 本票新增检查

- 注册声明与实际分配匹配：P 主 KV 8 份，附属区域和 D 主 KV 仍独立。
  同组 P 页描述符相同，副本 owner、成员和 NUMA 一致。
- 每个 P 成员在尚未分配的末槽写一个哨兵行。
  在既有 TP tick 汇总所有启动完成后，每个 P/D 成员通过自己的 NPU 映射读回组内全部哨兵。
  读取使用 `unidex_copy`，覆盖 D 读到非对应 P rank 写入的数据。
- 核对每个请求的设备写入计数与 fill 完成计数。
  默认分工覆盖完整页且无重叠；缺页时不能发布完整 ready。
- 复用原有内容、Graph、入口拒绝、容量拒绝、释放、超时和重启检查。
  只有两名 D 读者均停止、P 写者均清理后，Main 才能收到整侧 free_ack 并重新分配槽。

哨兵只在验收开关 `verify_prefix` 与 P 共享同时启用时执行。
它发生在请求接入前，不增加模型 prefix 验证的 BM 读取。
请求级内容验证沿用计算源快照与恢复后的 native HBM。

## 覆盖边界与报告

该入口标为 **K1-05 reduced functional acceptance**，沿用前票的两轮精简流程。
V3 使用独立题目答案；V8 在 CPU 上重放实际 NPU 注册的容量拒绝。
本轮不启动参考模型、不单独重启容量故障服务、不运行性能基线。
复制 smoke、真实模型功能结果与物理 NUMA 驻留证据分别记录。

BM 的 `local_mem_size` 在 join 时核对实际贡献量。
物理 NUMA 驻留另由现有页查询报告；驱动映射或容器权限不足时保留 unverified。
分配量减少不能改写成物理驻留已经验证；票据仍须由用户核对这些证据后确认关闭。

日志位于 `/home/cryang/log/mempool-min/k1-ticket05-001/{prefill,decode}/`。
回传 P 的 `summary.json`、`p-sharing-acceptance.json` 和两机终端结果。
失败时保留对应阶段日志。`target/`、`restart/` 内包含：

- `registrations.json`：区域 owner、成员、实际分配、NUMA 查询及跨写者哨兵结果。
- `responses.json`、`answers.json`、`snapshots.json`、`evidence.json`：请求与生命周期证据。
- `capacity-replay.json`、`ports-released.json`、`process-cleanup.json`：容量和清理证据。
- `target/timeout.json`：超时 fail-stop 证据。

报告中的 `user_accepted: false` 保留，用户确认另外记录。
2026-10-31，用户已确认db4b3ac1b9的k1-ticket05-002精简功能验收并关闭05。
[验收确认与边界](../.scratch/k1/verification/k1-ticket05-002-20261031/acceptance.md)保留全部limitations；重新执行需使用新的run-id。
