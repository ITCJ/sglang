# K1-01：显式 NUMA 映射与驻留验收

2026-10-29按用户要求，当前工程分支仅运行NUMA功能验收。
性能测试代码保留在`cryang/dev/mempool-min-baselinetest`；本分支只保留历史结果记录。

本票保持每侧 TP16、每 rank 私有 BM 池。`share=0`，P/D alive 主 KV、
P alive Index K、prefix 主 KV 与 Index K 都保留 16 份。
映射文件里的 `host` 使用本侧 `--mempool-nic tcp://HOST:PORT` 的 HOST，
同一物理主机使用同一地址。每侧文件必须恰有 rank 0–15。

启动配置先核对完整映射，再初始化 MF/BM。`create2` 仍按每个 P/D rank pair
创建，使用 `(1 << 7) | node` 指定本地 NUMA。每 rank 是自己四类本地
区域的 owner；D 只有 alive 主 KV。就绪、写入、读取、drain 和释放条件沿用阶段 0。

注册时查询本地 BM host VA 的页驻留，每区域取最多 17 个分布样本，额外抽样
包含对齐填充的整个池。Linux `move_pages(nodes=NULL)` 只查询，不迁移或读写 KV。
`/proc/self/numa_maps` 统计完全属于该池的 VMA；与其他分配混合的 VMA 不计作证据。
抽样全部命中配置节点且该池驻留字节数等于实际分配，才标记 `placement_verified`。
查询拒绝、驱动不暴露页节点、缺页或节点不符都保留原始证据并导致验收失败。
旧实验确曾出现 `/dev/davinci_manager` 不暴露驻留；不能假定本轮可观测。

`applied` 仅表示完整逐 rank 注册的 host、NUMA 与 owner 等于映射文件，
与物理驻留验证分开报告。GPU 注册范围来自已核对的 device VA；SDK 没有独立
注册耗时接口，该项保留 null，不以其他时间代替。

## NPU：每个容器一条命令

前置条件沿用已验收的 GLM-5.2 阶段 0 环境：P、D 各 16 张 Ascend NPU，
同一代码版本、相同路径的权重与 Python/torch-npu/sgl-kernel-npu/MemFabric 依赖，
已 source 本机 CANN/ATB 环境；可用 NUMA 0–7、网络接口与端口无占用。
本入口只管理自己启动的进程，日志写到 `/home/cryang/log/`。
需要允许本进程的页节点查询以及读取 procfs；失败不会自动改权限或改成通过。

两边各保存同样的 `/tmp/k1-config.json`，将占位符替换为实际值：

```json
{
  "prefill_host": "<P_IP>",
  "decode_host": "<D_IP>",
  "model_path": "/<LOCAL_GLM52_WEIGHTS>",
  "interface": "<NETWORK_INTERFACE>",
  "numa_nodes": [0, 1, 2, 3, 4, 5, 6, 7]
}
```

先在 P 启动，它会等待 D；再在 D 启动。两条命令的 run-id 必须相同且未使用。

```bash
# P 容器，仓库根目录
python3 ascend-mempool-test/scripts/run_mempool_min.py \
  --role prefill --mode numa-acceptance --config /tmp/k1-config.json --run-id k1-ticket01-001
```

```bash
# D 容器，仓库根目录
python3 ascend-mempool-test/scripts/run_mempool_min.py \
  --role decode --mode numa-acceptance --config /tmp/k1-config.json --run-id k1-ticket01-001
```

入口自动执行：

1. 三个非法映射通过生产 `MempoolConfig.from_server_args` 启动配置入口验证，
   不加载模型、不创建池；记录 `NUMA_MAP_DUPLICATE_RANK`、
   `NUMA_MAP_MISSING_RANK`、`NUMA_MAP_NODE_OFFLINE` 与非零退出码。
2. 默认映射 rank 0/1→0 … 14/15→7，随后交换 rank 0/2 的节点，保持每节点两个私有池。
3. 每种映射运行 A/B/C/E、V1–V10、入口拒绝、长生成、EOS、超时和双端清理。
4. 归档两种映射的注册、页驻留、拓扑、版本与功能验收结果。
   性能复测及逐段对照须在测试分支另行安排，当前入口不生成性能样本。

这是完整NUMA功能验收，**不是 smoke，也不包含性能验收**。`--describe` 只预览，不证明 NPU 行为。
全量过程多次重启大模型；中断会清理本侧自有进程，peer 错误会触发另一侧清理。
运行失败返回非零。驻留证据不齐时保留功能结果，最终总体失败。
历史性能数据可在 `.scratch/mempool-baseline/verification/` 查阅，不能作为本版本的复测证据。

## 回传与判据

根目录：`/home/cryang/log/mempool-min/k1-ticket01-001/`。
回传 P/D 两个子目录；重点先看 P 的 `summary.json`：

- `rejections/report.json` 与三份启动日志。
- `default/`、`swapped/` 内的 `full-acceptance/v1-v10.json`。
- `full-acceptance/target/numa-{mapping,registration,placement}.json`。
- 各阶段 `identity.json`、拓扑快照、P/D 日志与清理报告。

默认与交换映射都须 `applied`；全部 rank 的全部区域抽样正确。
按当前布局 P 每池 13 GiB、D 每池 1 GiB，故每 NUMA 分别为 26 GiB、2 GiB。
该数字只统计 BM 私有池，不能拿进程总 RSS 或整个节点 MemUsed 替代。
V1–V10 全过、错误退出和清理符合预期。
驻留不可观测项不得忽略；ticket 等待用户确认后才能关闭。

## CPU 与单独启动映射

CPU 测试环境需要仓库独立测试依赖（CPU torch、numpy、msgspec、aiohttp、zmq 等）。
以下入口自动设置源码路径、隔离 mempool 环境、运行测试并生成报告：

```bash
python3 ascend-mempool-test/scripts/verify_numa_plan.py --self-test \
  --output-dir /tmp/k1-numa-cpu-001
```

它只证明 CPU 配置、报告和启动命令行为。完整 CPU 回归用：

```bash
PYTHONPATH=ascend-mempool-test/src python3 -m pytest -q ascend-mempool-test/tests/unit
```

单独服务使用已有 `--mempool-rank-numa-map /path/to/local-map.json`，
或向 `run_service.sh` 传 `MEMPOOL_RANK_NUMA_MAP=/path/to/local-map.json`。
后者自动清除冲突的旧节点列表；直接 CLI 同时传旧环境变量会拒绝。
文件格式为 `[{"host":"<LOCAL_IP>","rank":0,"node":0}, ...]`，覆盖16个rank。

## 31/32现有环境命令

两机代码同步到本次移除perf后的相同版本。沿用上次验收配置，先31后32。
旧命令中的`--perf-rounds 5`已移除，勿再传入。

```bash
# 31 / P
cd /home/cryang/sglang && \
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode numa-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket01-002
```

```bash
# 32 / D
cd /home/cryang/sglang && \
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode numa-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket01-002
```

对应日志目录为 `/home/cryang/log/mempool-min/k1-ticket01-002/`。
