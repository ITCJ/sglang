# Ticket03 S4：Index K / aux 双机 NPU gate

S4 历史版本已通过用户 NPU 验收；S6 清理后的版本仍需复验。
MEMPOOL环境开关选择正式模式。本脚本显式构造正式模式的 pool，不启动模型或 SGLang server。

## 测试范围

`scripts/verify_pd_transfer.py` 使用实际 `NPUMLATokenToKVPool`、原 `KVArgs`、
`MetadataBuffers`、`AscendKVSender.send()`、继承的 transfer worker 及
`AscendTransferEngine`。构造 manager 时提供单请求测试状态，不启动服务后台线程；
原生注册表和状态通知通过独立 TCP 测试通道交付。

- P native HBM K/V 保留，注册列表只含 Index K 与 aux。
- Index K 实际层号 `[1,4,7]`；两段 chunk 从 P 页 `[1,4]` 写 D 页 `[5,2]`。
- 校验每一层的目标内容、未选页、全部 aux tensor、首 token 与 bootstrap room。
- 原 worker 消费队列、累计 chunk、只在末 chunk 发送 aux，结束后 outstanding 为零。
- 独立零页末 chunk 仍发送 aux；错误目标层号、aux 失败和复制中取消均不能报告 Success。
- 取消注入于同步复制入口，abort ACK 必须在复制返回、worker drain 后出现。
- 原 `MempoolPDControl` 验证两种 readiness 顺序、原 metadata gate 验证延迟可见性；
  native P buffer 复写时 P BM lease 仍占用，DONE/ACK 后双侧 slot 恢复。

只执行 TP16 拓扑中的一对逻辑 rank，不启动 TP collective。BM 完成事实由 fixture
提供，没有实际 BM 分配或读写；这不替代 S2 BM gate、S5 完整 scheduler/Graph/allocator
释放接线或 S6 TP16 模型验收。当前模型没有额外 `state_*` 组件，本 gate 的 state 列表为空；
必要的全部 handoff 数据使用原 MetadataBuffers 传输。

## 前置条件

1. 使用此前 S2/S3 gate 的同一 superpod 双机环境，各有一张空闲 NPU；加载相同 CANN、
   Python、torch/torch_npu、sgl-kernel-npu 和 MemFabric Hybrid 1.1.4 环境。
2. P/D 仓库切到同一提交。脚本会交换并核对 SHA 与包版本；工作区应无本地代码差异。
3. 从仓库根目录执行，确保 `python/` 中的本仓库 sglang 优先于旧安装。
   保持 Python 断言启用：不要传 `-O`/`-OO`，也不要设置 `PYTHONOPTIMIZE`。
4. 本入口固定 `ASCEND_MF_TRANSFER_PROTOCOL=sdma`，适用于此前验证过的 A3 SDMA 环境。
   `device_rdma` 需要分布式初始化，本脚本不支持；不要把此脚本结果用于该协议。
5. P 的 `18875`（TransferEngine store）和 `18876`（测试控制）端口空闲且双方互通。
   若改端口，两侧使用相同参数。现有服务可使用其他端口和设备；本脚本不停止服务。

脚本在独立进程内设置 `SGLANG_NPU_ENABLE_MEMPOOL=0`、
`SGLANG_NPU_ENABLE_SPARSE_KV_OFFLOAD=0`、`SGLANG_USE_FIA_NZ=0`，并由参数生成
`ASCEND_MF_STORE_URL=tcp://<P_IP>:18875`。正式模式通过 pool 构造参数选择。

## 执行命令

先在 P 执行，看到 `test listener ready` 后在 D 执行。将 `<P_IP>`、`<D_IP>`
替换成实际传输网卡地址；`--device-id 0` 可按空闲卡调整。

P：

```bash
set -o pipefail
PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}" python3 -u \
  ascend-mempool-test/scripts/verify_pd_transfer.py \
  --rank 0 --head-ip '<P_IP>' --local-ip '<P_IP>' --device-id 0 \
  --store-port 18875 --control-port 18876 --timeout 600 \
  --report /tmp/ticket03-s4-p.json 2>&1 | tee /tmp/ticket03-s4-p.log
```

D：

```bash
set -o pipefail
PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}" python3 -u \
  ascend-mempool-test/scripts/verify_pd_transfer.py \
  --rank 1 --head-ip '<P_IP>' --local-ip '<D_IP>' --device-id 0 \
  --store-port 18875 --control-port 18876 --timeout 600 \
  --report /tmp/ticket03-s4-d.json 2>&1 | tee /tmp/ticket03-s4-d.log
```

## 通过与失败判据

两侧都应出现六条 `PD_TRANSFER_PASS`，case 为：

| case | 应观察到的结果 |
| --- | --- |
| `bm_first` | KV_READY 先到；首 chunk 无 Success，末 chunk + aux 完成后才能准入 |
| `transfer_first` | 原传输先完成；metadata 可见后仍等待 KV_READY |
| `empty_last` | 零页末 chunk 只传 aux，原 worker 正常结束 |
| `bad_layout` | 目标层号错误，Failed，实际发送 0 字节 |
| `aux_failure` | Index K 成功、aux 注入失败；Failed，不能误报 Success |
| `cancel_inflight` | 复制期间取消，Failed；等同步复制和 worker drain 后发送 abort ACK |

最后均为 `ALL_CHECKS_PASSED`，退出码 0；两份 JSON 都有：

```json
{
  "success": true,
  "main_kv_registered_entries": 0,
  "main_kv_sent_bytes": 0,
  "index_k_layer_ids": [1, 4, 7]
}
```

main K/V 指标由注册/复制边界的地址范围断言保证；不是把 `kv_indices` 清空。
发送字节数须等于选中 Index K 页的总长度与成功发送 aux 的总长度之和。
失败 case 中预期出现拒绝或 Failed 日志；只有脚本对该 case 的状态、数据和 drain
断言均通过，才打印 PASS。初始化失败、超时、数据差异或断言失败均令整次 gate 失败。

双侧完成 FINISHED 握手并同步设备后才退出。失败后需保留日志，等待另一端退出，
从干净的新进程同时重跑两端；不要单独复用一侧旧进程。`--timeout` 约束测试 TCP 等待，
不保证能中断卡在 native SDK 内部的调用。

请回传 `/tmp/ticket03-s4-{p,d}.json` 和对应 `.log`。S4历史版本已获用户确认；S6清理后的版本需要重新验证。
普通模式的 CPU 传输回归随本次单测执行；旧 host/staging 的 NPU 回归可继续使用
[S3 资源 gate](README.md) 的 `local_offload`、`pd_decode_offload` 两个进程入口。
