# K1-ticket08：八段单份 prefix

布局为 P alive 主 KV / D alive 主 KV / P alive Index K / prefix 主 KV / prefix Index K：
`8 / 8 / 1 / 1 / 1`。40 页 prefix 分成 8 段，每段 5 页；默认 owner 为 rank 0/2/4/6/8/10/12/14。
主 KV 与 Index K 共用页描述符 `segment * 5 + offset`，Main 目录每页只保存一个位置。
BM 仍为前票的 32 参与者会话，P follower 不贡献物理内存，身份探针使用 owner 的独立保留槽。

## 双机运行

两机使用相同提交、配置和模型，先启动 P，再启动 D。默认复用前票配置：

```json
{
  "prefill_host": "<P_IP>",
  "decode_host": "<D_IP>",
  "model_path": "/<LOCAL_GLM52_WEIGHTS>",
  "interface": "<NETWORK_INTERFACE>",
  "numa_nodes": [0, 1, 2, 3, 4, 5, 6, 7]
}
```

P 容器，仓库根目录：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode prefix-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket08-001
```

D 容器，仓库根目录：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode prefix-sharing-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket08-001
```

增加 `--describe` 只打印有效配置、预算和阶段，不启动服务。失败后保留日志，重测使用新 run-id。
模式自动开启 P/D 主 KV、P Index K、prefix 共享，设置 40 页、命中与内容验证。
BM 预算 P **96 GiB**、D **8 GiB**；P 验收 CPU 快照和临时恢复数据另预留 **32 GiB**。
预算不等于实测 RSS、UB capture 或物理 NUMA 驻留。

入口自动准备、等待服务、请求、检查结果、收集报告并清理自身进程，失败返回非零。
store 端口仍为 P 的 19000，NIC 范围仍为 25670–26181；沿用原有 HTTP/控制/传输端口。
清理由 SGLang 默认父进程及进程组完成，没有逐 rank 主动释放协议。

## 实现与可选配置

- 默认 `prefix_admit_mode: "owners"`：八个 owner 并行写各自的段，主 KV 从本组 alive 读取。
  Index K 来自 NUMA0 单份区域，其他段对此读取会跨 NUMA。
- `prefix_admit_mode: "single_rank"` 配合 `prefix_admit_rank`（默认 0）可集中保存；CPU 已覆盖该路径。
- `prefix_start_segment: null` 按新请求轮转；整数 0–7 固定起始段。段满时取空闲页最多的段，平局取段号较小者。
- `prefix_read_assignment: "auto"` 在共享模式解析为 `by_segment`；另支持 `round_robin`、`contiguous` 对照。
- `prefill_index_writers` 仍支持 `[0]` 或 `[0, 1]`。默认硬件入口使用 `[0]`。
- 普通 `serve` 需在配置中开启 `prefill_shared`、`decode_shared`、`prefill_index_shared`、`prefix_shared`，
  设置 `prefix_pages: 40`、`match_prefix: true`。`verify_prefix` 可关闭。

分片解析自己的设备映射，按真实段的 5 页层步长提交现有 `unidex_copy`。
load_back 每页只由一个读者读取，全部 rank 参加 all_gather；零页 rank 只贡献通信 padding。
fill 从全部八段直接填回八份主 KV 和一份 Index K，组内分工依据完整 prompt 长度。
全部 owner 完成、每页单写者覆盖成立后才能发布 prefix、释放 alive 源。
线程执行器与批量异步搬运仍属于 ticket11。

所有 P 分片访问全部 prefix 段。BM 会话实际映射涵盖 32 个参与者，设备地址范围和映射者另行记录；
映射者数不等于物理副本数。SDK 没有独立注册计时接口，`registration_seconds` 保持 null 并附原因。
NUMA planner 的 `--router-config <effective-config.json>` 可预览相同几何、八段和贡献量，仍需提供原有 topology/NUMA 输入。

## 验收范围与回传

两轮 target/restart 使用同一配置，每轮 A/B/C/E、长生成、提前 EOS 六个请求。
首轮完成请求后注入超时检查 fail-stop；第二轮同端口重启并检查正常清理。
07 的三题逻辑实验由独立模式选择，本入口不追加。单 rank admit 的硬件对照不包含在默认两轮中。

| 项目 | 检查与边界 |
|---|---|
| 命中与容量 | A/B/C/E 命中 0/4096/3968/0；prefix 占用 32→40→40→40；目录每页一个位置 |
| 内容 | 主 KV 和 Index K 分别关联实际源写者摘要；正常恢复后的 HBM 独立比较；错误摘要必须失败 |
| 拷贝与就绪 | admit 单写者/字节，load_back 单次读/段/字节，fill 八份主 KV+一份 Index K；完成与释放顺序 |
| 注册 | 8/8/1/1/1、每段 owner/大小、映射范围、P follower=0；实际分配与注册一致 |
| V3 | 独立逻辑答案；没有另外启动正式服务进行冷 A/C 稳定性对照 |
| V8 | 真实 NPU 注册的 CPU 容量拒绝重放；没有单独启动容量故障 worker |
| 未验证 | 不增加请求级 BM 读回；不能据此宣称直接比较八份 fill DRAM 内容、物理 NUMA 驻留或性能通过 |

原始逐区域 NUMA 采样随注册保存，权限不足或不可观测时保留未验证；不以逻辑映射代替物理证据。
这是一套精简功能验收，不标为原始 V1–V10 全覆盖。用户确认后才能关闭 08，随后才解锁 11。

日志目录为 `/home/cryang/log/mempool-min/k1-ticket08-001/{prefill,decode}/`。
回传两机终端结果、P 的 `summary.json` 与 `prefix-sharing-acceptance.json`。
每轮保存 `registrations.json`、`snapshots.json`、`responses.json`、`answers.json`、`evidence.json`、
容量重放、超时、清理与端口释放报告；报告包含实际页列表、源写者、各区域字节和按段行数。
失败时同时回传对应 P/D 日志及有效配置。自动报告始终保留 `user_accepted: false`。
