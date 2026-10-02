# Ticket 02：小容量真实 KV 读回

本轮在真实 GLM-5.1 D 服务的 selected top-k KV 处，额外从 BM 读回并逐元素比较
BF16 值。attention 仍使用原 selected KV；hostSHM 和原 main-KV transfer 保留到 03。
NUMA 的容量/分配长尾已经移到 09，本轮使用已启动成功的小配置。

## 更新和启动

P/D 使用本次同一提交，分别保存 `git rev-parse HEAD` 和 `git diff --stat`。
以下基于已跑通的 `glm51dis.sh`，保留原来的本地权重路径、DeepEP、网卡和 router 配置。
在脚本实际启动 Python 的 shell 中设置（P/D 均可设置；读回只在 D 执行）：

```bash
cd /home/cryang/sglang
export SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD=1
export SGLANG_NPU_ENABLE_MEMPOOL=1
export SGLANG_NPU_MEMPOOL_READBACK=1
export SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE=0,2,4,6
export SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT=600
```

把两侧现有 `sglang.launch_server` 的对应参数替换为下面的小容量值，避免同时保留旧值：

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
P: /tmp/mempool-02-readback-p.log
D: /tmp/mempool-02-readback-d.log
```

待双方 mapping/control ready、D Graph capture 完成后，启动已有 PD router。
详细端口和旧服务启动约定见 [README 的服务 gate](README.md#启动参数增量)。

## 三个请求及日志检查

在可访问 router 的机器运行，把 `<ROUTER_IP>:<ROUTER_PORT>` 换成已有地址：

```bash
python3 ascend-mempool-test/scripts/verify_shadow_service.py requests \
  --url http://<ROUTER_IP>:<ROUTER_PORT> \
  --decode-tokens 32 --timeout 900 \
  --output /tmp/mempool-02-readback-requests.json
```

这会依次发送首 token 结束（零 decode）、实际 decode、下一请求复用三项。
预期三条 `PASS case=...` 和 `REQUESTS_PASSED`；同时检查生成文本。

等每个 rank 的 `RELEASE_ACK` 后，把 P/D 完整日志汇集在同一台机器，再运行：

```bash
python3 ascend-mempool-test/scripts/verify_shadow_service.py check-logs \
  --prefill-logs /tmp/mempool-02-readback-p.log \
  --decode-logs /tmp/mempool-02-readback-d.log \
  --requests 3 --require-readback --readback-layers 78 \
  --output /tmp/mempool-02-readback-result.json
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
- 每个 rank 至少两个不同 decode attempts 实际复用了同一组 row、P slot 和 D slot；
  报告的 `reuse` 列出对应 room/attempt 和物理位置。

HTTP 完成不代表 DONE/ACK 已完成。如果提示 `no actual row/P/D slot reuse`，
等全部 `RELEASE_ACK` 后再次发送这三个请求，requests JSON 换一个输出文件名，
保留同轮完整日志再执行 `check-logs`；不能仅凭串行请求推断已验证复用。

出现值不一致、正索引超出实际写入范围、缺层或缺 rank 时不通过；缺少读回变量、
全 padding、只有 warmup、缺 release ACK 或协议 fault 也不能通过。失败时日志检查
仍会保存 `status=failed` 及错误信息。

`mempool KV readback failed` 包含 TP rank、layer、request room/rid/attempt、逻辑
position、feature、P/D slot。发现差异后停止本轮，不继续用该进程验证复用。
保留失败完整日志及 JSON，避免仅截取最后几行。

## 本轮的证据边界

数值比较在设备执行，不在 capture 中调用 `.cpu()`、`.item()` 或 host synchronize。
每 forward 在同一提交 stream 上保存小型比较快照并记录完成事件；事件完成后才由
CPU 校验结果。后续 replay 复用 scratch 不会覆盖尚未消费的快照。D drain 涵盖读回，
正常释放前才输出汇总；P 的 BM slot 仍保持到对应 DONE/ACK。

短 context 下，2048 是选择宽度，许多列是 padding；通过不等于验证 2048 个有效 token，
也不等于完成 8192/16384 容量测试或 AIME 精度验收。P/D 不同 slot、zero-valid source
和 row reuse 有 CPU 回归及此前独立 gate 支撑；本轮真实服务会记录实际 slot，不强行
改变分配器制造不同 slot。

请回传两侧代码版本、完整 P/D 日志、requests JSON 和 result JSON。
本地 CPU 检查无法替代远端 BM/真实模型/NPU Graph 的证据；ticket 02 在本轮用户 NPU
验收确认后关闭，再进入 03 的数据路径切换与 hostSHM/main-KV transfer 移除。
