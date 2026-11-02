# K1-ticket12：page first 布局验收

五类主存区域使用`[物理页, 层, 页内token, head, feature]`，副本保持8/8/1/1/1。
两机使用同一提交、配置及模型。该入口复用08的两轮六例，不恢复性能埋点。
NPU验收尚待用户执行；本机CPU结果不代表硬件通过。

## 双机自运行入口

先启动P，再启动D；在各自容器的仓库根目录执行。
配置沿用08，至少包含`prefill_host`、`decode_host`、`model_path`、`interface`和8个`numa_nodes`。
将下列配置路径替换成实际JSON路径，两侧使用同一个run-id。

P：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role prefill --mode page-first-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket12-001
```

D：

```bash
bash ascend-mempool-test/scripts/run_mempool_min.sh \
  --role decode --mode page-first-acceptance \
  --config /home/cryang/mempool-min-ticket01-3132.json --run-id k1-ticket12-001
```

添加`--describe`可在不启动服务的情况下核对配置、预算和两轮安排。
入口自动准备、检查双机版本与环境、启动、等待就绪、发请求、核对结果和收集报告；
使用既有进程组/父进程路径清理自身进程，任何失败返回非零。失败后用新run-id重测。

## 执行顺序

1. 模型启动前，每机在NPU 0运行一次拷贝检查：小几何、真实P主KV和Index K几何。
   最大设备内存测试区11,777,605,632 B，检查跨页、最后槽、换槽及零有效行的图重放。
   这是独立设备内存测试区，不创建或直接读回BM；读写结果再由物理张量维度独立核对。
   进程退出后释放测试区，随后启动已有P/D/mixed fetch组件smoke。
2. 第1轮服务运行A/B/C/E、长生成和提前EOS六例，随后注入超时验证fail-stop。
3. 第2轮同端口重启，复用六例并核对正常释放、槽复用及双端清理。
4. 每轮核对实际注册的page-first维度、五页prefix段、page_bytes及启动签名。
   合并报告仍保留`user_accepted: false`，等待用户确认。

BM预算沿用08：P合计96 GiB、D合计8 GiB，P验收源快照另预留32 GiB主存。
独立NPU检查在模型之前运行，需要NPU 0有至少约11 GiB可用设备内存及运行余量。
这些是分配预算，不是物理NUMA驻留证据或性能结果。

## 判据和证据边界

- 主KV每页11,501,568 B；Index K每页688,128 B，几何由实际模型推导。
- A/B/C/E命中0/4096/3968/0；prefix占用32→40→40→40；每页一个不透明描述符。
- B：主KV load_back为368,050,176 B、fill为2,944,401,408 B，Index K两项各22,020,096 B。
  C的31页在各段分布不同，通信padding不计主存读取；字节口径与08一致。
- 正确性沿用计算时源快照、正常load_back后的设备内容及写读记录。没有新增BM内容读回。
- ready/free、每页单写者、零页读者、非连续原生目标页、Graph、账本与清理继续核对。
- 物理NUMA驻留、直接比较alive/fill主存、性能不属于本票。single_rank admit及多Index K写者默认仅CPU覆盖。
- 这是精简功能验收；V3使用独立逻辑答案，V8用真实注册CPU重放，不宣称原始V1–V10全部覆盖。

## 回传

日志在`/home/cryang/log/mempool-min/k1-ticket12-001/{prefill,decode}/`。
回传两侧终端结果、`summary.json`、`page-first-acceptance.json`及`page-first-copy/report.json`。
每轮保留`registrations.json`、请求/答案/内容/字节证据、超时、清理和端口释放记录；失败时同时回传对应P/D日志。
库分配、映射范围与物理驻留继续分别记录。用户确认本票NPU验收后才关闭12、解锁11。

## 003后的端口等待修复与轻量验证

用户已批准：只有绑定报EADDRINUSE且该地址的TCP记录非空、全部为TIME_WAIT时，才等待真正可绑定。
同一阶段所有端口共享operation_timeout（现有配置600秒）；监听、已建立连接、未知状态或超时仍失败。
每个端口的首末快照、耗时及最终状态保存在对应阶段的`port-release-<port>.json`。
该修复不证明003原现场一定是TIME_WAIT，也不修复memfabric退出析构异常。

先只在32容器仓库根目录运行Linux TCP验证，不启动NPU或模型：

```bash
python3 ascend-mempool-test/scripts/verify_port_release.py \
  --timeout 90 \
  --report /home/cryang/log/mempool-min/k1-ticket12-port-release/report.json
```

入口创建未开启SO_REUSEADDR的回环连接并主动关闭，要求观察到TIME_WAIT阻塞后真正重绑成功，
否则返回非零；常见等待约一分钟，最大90秒。回传终端输出及report.json。
本机Mac只验证了控制流程与状态策略；此Linux真实等待验证尚待用户执行。
