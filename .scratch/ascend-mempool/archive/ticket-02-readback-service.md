> 历史归档：这是Ticket02验收时的shadow操作说明。S6.2已删除对应模式、READBACK和脚本，以下命令不适用于当前版本；当前入口见[正式服务验收](../../../ascend-mempool-test/FORMAL_SERVICE.md)。

# Ticket 02：小容量真实 KV 读回

**状态（2026-10-03）：已获用户确认验收，ticket 02 已关闭。** 本页保留复测入口，
实现路径和实测结果见[02总结](../ticket-02-summary.md)。
下一开发阶段为[03正式attention切换](../issues/03-prefill-direct-offload.md)。

本轮在真实 GLM-5.1 D 服务的 selected top-k KV 处，额外从 BM 读回并逐元素比较
BF16 值。attention 仍使用原 selected KV；hostSHM 和原 main-KV transfer 保留到 03。
NUMA 的容量/分配长尾已经移到 09，本轮使用已启动成功的小配置。

## 更新和启动

P/D 使用同一提交，至少包含读回实现 `c4ec7c6b67`；分别保存 `git rev-parse HEAD`
和 `git diff --stat`。同时更新 `ascend-sglang-script` 仓库的 `main` 分支，
至少包含脚本提交 `8074c0c`。离线检查器使用 `fb9a6cde5b` 或更新版本，
避免旧版将request row轮换误判为物理slot未复用。

更新后的 `ascend-sglang-script/pd-disaggregation/glm51mempool.sh` 已包含本页全部
启动增量。核对脚本中的 P_IP、D_IP、MODEL_PATH 和网卡名后，在各自机器的脚本仓库执行：

```bash
# P 机器先执行；将 <P_IP> 换成脚本中的 P 地址。
LOCAL_HOST1='<P_IP>' bash pd-disaggregation/glm51mempool.sh

# D 机器随后执行；无需等 P 完全 ready。
LOCAL_HOST1='<D_IP>' bash pd-disaggregation/glm51mempool.sh
```

脚本的原 TransferEngine store 地址 `ASCEND_MF_STORE_URL` 自动使用 `P_IP[0]:24670`。
脚本默认日志目录为 `/tmp/mempool-02-readback-small`，P 写 `p.log`、D 写 `d.log`。
可在命令前用 `LOG_DIR=/tmp/<新的目录>` 覆盖；每次启动覆盖该目录内的本侧日志。
下文命令使用默认目录。每轮保留完整日志，读日志时再筛选。

若继续使用已有 `glm51dis.sh`，保留本地权重、DeepEP、网卡和 router 配置，
在实际启动 Python 的 shell 中加入下面的环境变量（两侧均设置；读回只在 D 执行）：

```bash
cd /home/cryang/sglang
export SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD=1
export SGLANG_NPU_ENABLE_MEMPOOL=1
export SGLANG_NPU_MEMPOOL_READBACK=1
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
export SGLANG_NPU_MEMPOOL_DIAGNOSTICS=0
export SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=600
```

`DIAGNOSTICS=0` 关闭周期性 WAIT、内存/栈快照，以及 mempool 主动提升 MF INFO 的操作。
基本的 `[MEMPOOL_INIT] BEGIN/END/FAIL/READY` 和验收日志仍保留。保持 SGLang 默认
INFO 日志级别；全局改成 WARNING/ERROR 会丢失数值和生命周期验收证据。

两侧 `sglang.launch_server` 的参数应为以下小容量值。新版 `glm51mempool.sh` 已设置；
其他脚本须替换对应参数，避免同时保留旧值：

```text
--context-length 1024
--max-prefill-tokens 512
--max-running-requests 16
--mempool-prefill-host <P_IP>
--mempool-bootstrap-port <P_BOOTSTRAP_PORT>
--mempool-base-port 19000
--mempool-pool-id 104
--mempool-prefill-capacity 512
--mempool-decode-capacity 512
--mempool-timeout 600
```

`<P_BOOTSTRAP_PORT>` 与 P 已有的 `--disaggregation-bootstrap-port` 完全一致
（此前样例为 8995）。P 的 `--mempool-nic` 使用
`tcp://<P_IP>:25670`，D 使用 `tcp://<D_IP>:25670`。

维持 TP16/DP1/PP1/CP1、BF16、Ascend attention/transfer backend、关闭 radix cache；
P 用 `--disable-cuda-graph`，D 用 `--cuda-graph-bs-decode 16`。
不启用 MLAPO、draft、prefix 复用或 two-batch overlap。普通 scheduler overlap 可保持开启。
保持 sparse top-k 宽度 2048。

以 78 层、16 slots、576 维 BF16 计算，每侧每 rank 的 BM 贡献为 1 GiB，
每机合计 16 GiB。每个 D worker 的 16×2048 读回 scratch 约 36 MiB，按层顺序复用；
另有 Graph 运算中间张量及约 104 KiB 的每 forward 结果快照。原 hostSHM 仍占内存。

先启动 P，随后启动 D；P 等待 D join 时不用等 P 完全 ready 才起 D。
两侧使用新日志文件，不追加旧运行记录，例如：

```text
P: /tmp/mempool-02-readback-small/p.log
D: /tmp/mempool-02-readback-small/d.log
```

待双方 mapping/control ready、D Graph capture 完成并进入服务循环后，另开终端启动 router：

```bash
python3 -m sglang_router.launch_router \
  --pd-disaggregation --policy round_robin \
  --prefill 'http://<P_IP>:8000' 8995 \
  --decode 'http://<D_IP>:8001' \
  --host 127.0.0.1 --port 6699 --mini-lb
```

这里的 8995 与脚本的 P bootstrap port 一致；若改了端口需一并更新。
后面的请求命令在 router 所在机器运行，可使用 `http://127.0.0.1:6699`。
已有 router 可直接沿用。详细旧服务启动约定见 [02历史验收记录](../ticket-02-summary.md)。

## 三个请求及日志检查

在 sglang 仓库根目录运行；示例在 router 所在机器访问 6699 端口，按实际部署调整：

```bash
mkdir -p /tmp/mempool-02-readback-small
python3 ascend-mempool-test/scripts/verify_shadow_service.py requests \
  --url http://127.0.0.1:6699 \
  --decode-tokens 32 --timeout 900 \
  --output /tmp/mempool-02-readback-small/requests.json
```

这会依次发送首 token 结束（零 decode）、实际 decode、下一请求复用三项。
预期三条 `PASS case=...` 和 `REQUESTS_PASSED`；同时检查生成文本。

等每个 rank 的 `RELEASE_ACK` 后，把 P/D 完整日志汇集在同一台机器，再运行：

```bash
python3 ascend-mempool-test/scripts/verify_shadow_service.py check-logs \
  --prefill-logs /tmp/mempool-02-readback-small/p.log \
  --decode-logs /tmp/mempool-02-readback-small/d.log \
  --requests 3 --require-readback --readback-layers 78 \
  --output /tmp/mempool-02-readback-small/result.json
```

如每 rank 独立日志，可在对应选项后列出 16 个文件。不要同时提供合并日志和重复的
worker 日志。当前 gate 使用 NPU device ID 0–15，与既有部署相同。

通过时打印 `SHADOW_READBACK_PASSED`，报告 `status=shadow_readback_passed`。
gate 同时要求：

- P/D 全 16 ranks 完整 acquire/binding/ready/drain/DONE/ACK，最后每 rank 16 slots 可用。
- D 全 16 ranks 的 Graph capture 和真实 replay。
- 每个请求的全部 D ranks 有 `mempool readback_result`，包含 room/attempt/row 和 P/D slots。
- 零 decode 请求只报告 `zero_decode`，不虚构 KV 比较。
- 后两个请求逐层检查次数等于 `78 × forwards`，写入和比较 forward 数一致，
  有真实 readback replay，top-k 宽度始终为 2048；每层都有有效 KV，且每 rank 都有
  P、D、prompt 尾行和 D position 0 的比较证据。
- 每个 rank 至少两个不同 decode attempts 实际复用了同一组 P slot 和 D slot；
  报告的 `reuse` 列出对应 room/attempt、物理位置及两次的 `rows`。
  D 的 request row 按 FIFO 分配，释放后放回队尾，因此 row 从 2 换到 3 等正常轮换
  不影响物理 slot 复用。`row_reused=false` 只表示这两个请求未复用 request row。

旧版检查器把 row 和 P/D slot 绑成一组，可能误报 `no actual row/P/D slot reuse`。
更新检查脚本后，先对现有完整日志重新执行上面的 `check-logs`，无需重启服务或重新
发送请求。只有物理 P/D slot 确实复用才会通过，不会跳过数值、Graph 或释放检查。

HTTP 完成不代表 DONE/ACK 已完成。如果新版提示 `no actual P/D slot reuse`，
等全部 `RELEASE_ACK` 后再次发送这三个请求，requests JSON 换一个输出文件名，
保留同轮完整日志再执行 `check-logs`；不能仅凭串行请求推断已验证复用。

如果在P机检查从D复制的日志，每次补请求后都要重新同步最新P/D日志，保留本次服务的
启动、capture及最后ACK部分。2026-10-02的实际排查中，D已跑完三轮九请求，P上的
`d.log`却仍只含第一轮；仅重复发请求不会更新这个副本。该次最新rank0汇总应为
`zero_decode: 3, passed: 6`。`--requests`是最少请求数，此类三轮重检可设为9，
避免旧的三请求副本通过数量检查；这不是以后每次复测都必须发送九个请求。

出现值不一致、正索引超出实际写入范围、缺层或缺 rank 时不通过；缺少读回变量、
全 padding、只有 warmup、缺 release ACK 或协议 fault 也不能通过。失败时日志检查
仍会保存 `status=failed` 及错误信息。

`mempool KV readback failed` 包含 TP rank、layer、request room/rid/attempt、逻辑
position、feature、P/D slot。发现差异后停止本轮，不继续用该进程验证复用。
保留失败完整日志及 JSON，避免仅截取最后几行。

## 怎样读当前输出

| 输出 | 表示什么 | 接下来关注什么 |
| --- | --- | --- |
| `[MEMPOOL_INIT] READY` | 本 worker 的 BM/runtime 初始化完成 | 继续等待 control 和整个服务 ready |
| `mempool mapping_ready`、`POOL_HELLO`/`POOL_READY` | 各 rank 映射和 P/D 控制握手的证据 | 两侧全 16 ranks 到齐，由检查器核对 |
| `mempool graph_captured` | D 已捕图 | 请求后须出现真实 `graph_replay` |
| `PASS case=zero_decode/decode/reuse`、`REQUESTS_PASSED` | 三个 HTTP 请求成功返回指定 token 数 | 仍需等待释放及离线日志检查；查看生成文本 |
| `mempool readback_result ... data=...` | D 对该请求、本 rank 的已完成读回汇总 | 看 status、数值覆盖及实际位置；这条在 drain 后输出 |
| `event=DONE`、`event=RELEASE_ACK` | 控制协议完成释放确认 | 最后全 ranks 的 `free=16`，并有实际复用证据 |
| `SHADOW_READBACK_PASSED` | 离线检查的所有数值、Graph、生命周期和复用条件通过 | 保存 requests/result JSON 与完整 P/D 日志 |

`readback_result` 中，首 token 结束的请求应为 `status="zero_decode"`、`forwards=0`，
这属于正常成功。真实decode请求应为 `status="passed"`，计数按实际提交的forward核对：
`written_kv=forwards`、`layer_checks=layers×forwards`。本次32-token请求的实测为
`forwards=replay_forwards=written_kv=32`、`layers=78`、`layer_checks=2496`；
不能固定用输出token数减一代替设备工作计数。检查器还要求 `replay_forwards>0`、
`prompt_kv/decode_kv/prompt_boundary_kv/decode_first_kv>0`、
`min_topk=max_topk=2048`。`prompt_slot/decode_slot` 用于核对下一请求真实存储复用；
`row` 是独立的请求表位置，不能用其变化判断 mempool slot 是否复用。
比较失败会抛出异常并记录 `mempool KV readback failed`，不会用成功汇总掩盖差异。

快速看关键行（D 机器，或已汇集两侧日志的机器；只筛选显示，不改原日志）：

```bash
rg 'mempool (mapping_ready|graph_captured|graph_replay|readback_result)|event=(DONE|RELEASE_ACK)|KV readback (failed|mismatch)|Traceback' \
  /tmp/mempool-02-readback-small/d.log
```

最终以带 `--require-readback` 的 `check-logs` 为准；单条某 rank 的 `status="passed"`
只覆盖该请求和该 rank，不能代表全 16 ranks 通过。

## 本轮的证据边界

数值比较在设备执行，不在 capture 中调用 `.cpu()`、`.item()` 或 host synchronize。
每 forward 在同一提交 stream 上保存小型比较快照并记录完成事件；事件完成后才由
CPU 校验结果。后续 replay 复用 scratch 不会覆盖尚未消费的快照。D drain 涵盖读回，
正常释放前才输出汇总；P 的 BM slot 仍保持到对应 DONE/ACK。

短 context 下，2048 是选择宽度，许多列是 padding；通过不等于验证 2048 个有效 token，
也不等于完成 8192/16384 容量测试或 AIME 精度验收。P/D 不同 slot、zero-valid source
和 row reuse 有 CPU 回归及此前独立 gate 支撑；本轮真实服务会记录实际 slot，不强行
改变分配器制造不同 slot。三请求 gate 要求物理 P/D slot 复用；若 `row_reused=false`，
这轮不能作为真实服务 request row 复用的硬件证据。

后续复测保留两侧代码版本、完整P/D日志、requests JSON和result JSON。
本地CPU检查无法替代远端BM/真实模型/NPU Graph证据。02已于2026-10-03经用户确认
关闭；03负责数据路径切换与hostSHM/main-KV transfer移除，其验收单独记录。
