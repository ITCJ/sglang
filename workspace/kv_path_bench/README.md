# A3 MLA KV 传输路径测试

独立于模型和 HiCache，比较同一份 BF16 MLA KV 写入同一组 NPU L1 page 的耗时：

| 输出中的路径 | 计时范围 |
| --- | --- |
| `L2->L1` | 本机 Host L2 经 Ascend 搬运 kernel 写入 L1 |
| `L3->L2->L1` | Mooncake Store 读入本机 Host，拆分两个分量，再经同一 kernel 写入 L1 |
| `L3->NPU staging->L1` | Store 读入 NPU 暂存，重排到同一 L1；包括暂存和重排时间 |

数据按 DeepSeek V3.1 的 61 层、每页 128 token、BF16、每 token 512 维压缩 KV + 64 维 RoPE 构造。一页一个 Store 对象（约 8.99 MB），对象内按 `[layer, token, 512+64]` 排列；Host L2 和 NPU L1 则各自将 512/64 分别存入两个缓冲区。这里的 `v_buffer` 是 RoPE，不是传统注意力里单独的 V。

在远端 A3 上启动 Store（保持进程运行）：

```bash
python3 workspace/kv_path_bench/store_server.py --local-ip <远端IP> --tokens 1024
```

看到 `DATA_READY` 后，在本机 A3 上运行：

```bash
python3 workspace/kv_path_bench/kv_transfer_bench.py --local-ip <本机IP> --master-ip <远端IP> --tokens 1024
```

两端需有 `torch`、`torch_npu`、`mooncake.store`，本机还需 `sgl_kernel_npu`；使用相同的 `--tokens`、`--prefix` 和端口。两端都配置了 `ascend` Store 传输和 Fabric 环境变量。结果写入本机 `kv-transfer-results.json`，包含每次样本、耗时中位数、P95、按原始 KV 字节数计算的有效 GB/s 和额外暂存内存。预热 2 次、记录 10 次；每次计时包含最终 NPU 同步，数据校验在计时后进行。内存分配、注册和远端数据准备不计时。

默认仅测 1K token（约 69 MiB）；扩大数据量需两端同步调整 `--tokens`，16K 时远端还需 `--segment-gib 2`。暂存内存会随规模增长，先确认可用内存。`--skip-direct` 可跳过第三条路径；如果 NPU 缓冲区不能注册到当前 Store，结果中将该路径标为 `unavailable`，不把 Host 中转冒充直达。配置 `ascend` 并不单独证明实际使用了 Fabric，仍需结合 A3 环境的传输日志或计数器确认。

本地无需 NPU 的数据布局检查：

```bash
python3 -m unittest discover -s workspace/kv_path_bench -p 'test_*.py'
```
