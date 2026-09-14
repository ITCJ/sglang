# A3 MLA KV 传输路径测试

本目录的可行性和性能脚本统一使用非默认端口：master RPC 为 `19271`，管理/metrics 为 `19273`，集中定义在 `bench_ports.py`。两端更新代码后，原启动命令不变。非默认端口仍可能被占用；若冲突，修改该文件并同步两端，不停止其他任务的服务。这两个配置只控制 master 的监听端口。

独立于模型和 HiCache，比较同一份 BF16 MLA KV 写入同一组 NPU L1 page 的耗时：

| 输出中的路径 | 计时范围 |
| --- | --- |
| `L2->L1` | 本机 Host L2 经 Ascend 搬运 kernel 写入 L1 |
| `L3->L2->L1` | Mooncake Store 读入本机 Host，拆分两个分量，再经同一 kernel 写入 L1 |
| `L3->NPU staging->L1` | Store 读入 NPU 暂存，重排到同一 L1；包括暂存和重排时间 |

数据按 DeepSeek V3.1 的 61 层、每页 128 token、BF16、每 token 512 维压缩 KV + 64 维 RoPE 构造。一页一个 Store 对象（约 8.99 MB），对象内按 `[layer, token, 512+64]` 排列；Host L2 和 NPU L1 则各自将 512/64 分别存入两个缓冲区。这里的 `v_buffer` 是 RoPE，不是传统注意力里单独的 V。

远端不能复制命令：先在两端仓库各手输一次 `git pull --ff-only`。claim 设备之前，分别手输一条不初始化 NPU 的命令；脚本会打印 commit，需确认两端与交接的 commit 相同：

```bash
python3 workspace/kv_path_bench/preclaim_check.py store
python3 workspace/kv_path_bench/preclaim_check.py client
```

两行分别在 Store 端、客户端运行，不是在一台机器上连续运行。看到 `PRECLAIM_OK` 才继续。此检查不连接两端、不验证 Fabric，也不替代 claim 后的小规模传输测试。仓库不保存真实 IP、容器名和凭据。

在远端 A3 上启动 Store（保持进程运行）：

```bash
python3 workspace/kv_path_bench/store_server.py --local-ip <STORE_IP> --tokens 128
```

看到 `DATA_READY` 后，在本机 A3 上运行：

```bash
python3 workspace/kv_path_bench/kv_transfer_bench.py --local-ip <CLIENT_IP> --master-ip <STORE_IP> --tokens 128
```

两端需有 `torch`、`torch_npu`、`mooncake.store`，本机还需 `sgl_kernel_npu`；使用相同的 `--tokens`、`--prefix` 和端口。两端都配置了 `ascend` Store 传输和 Fabric 环境变量。结果写入本机 `kv-transfer-results.json`，包含每次样本、耗时中位数、P95、按原始 KV 字节数计算的有效 GB/s 和额外暂存内存。预热 2 次、记录 10 次；每次计时包含最终 NPU 同步，数据校验在计时后进行。内存分配、注册和远端数据准备不计时。

两端先确认设备已 claim、模型已停止。上述命令先测一页；成功校验后两端再同步使用 `--tokens 1024`（约 69 MiB）。扩大数据量时，16K 远端还需 `--segment-gib 2`。暂存内存会随规模增长，先确认可用内存。`--skip-direct` 可跳过第三条路径；如果 NPU 缓冲区不能注册到当前 Store，结果中将该路径标为 `unavailable`，不把 Host 中转冒充直达。配置 `ascend` 并不单独证明实际使用了 Fabric，仍需结合 A3 环境的传输日志或计数器确认。

本地无需 NPU 的数据布局检查：

```bash
python3 -m unittest discover -s workspace/kv_path_bench -p 'test_*.py'
```

## 可行性验证（不计时）

当前先测 Host 路径：客户端原命令末尾加 `--host-only`，Store 命令不变。`H0` / `H1` 分别表示 small / max 的 `L3->Host->L1` 逐页校验通过；不代表 NPU 直达通过。默认双路径模式也会先输出 Host 成功码，再分配和注册 NPU 暂存区，最后两条都通过才输出 `P0` / `P1`。`F4` 表示 NPU 暂存分配或注册失败。NPU 分配调查见 [交接文档](NPU_FABRIC_HANDOFF.md)。

Python、底层库和子进程的标准输出/错误均写入日志；终端只显示短结果码。

可行性脚本用 `mooncake.store.BufferPool` 从 `setup()` 已注册的 1 GiB ADXL Host 缓冲区借用接收暂存区（最多 8 页），不再额外分配并注册 Host 内存。测试结束先归还缓冲区再关闭 Store；不改变批量读取、拆分或 L1 搬运过程。该接口已核对官方 `v0.3.12.post1` 源码，A3 运行仍待验证；旧性能脚本的分配方式尚未同步。

claim 设备、停止模型后，先在 Store 端运行（出现 `S0` 后保持运行），再在客户端运行：

```bash
python3 workspace/kv_path_bench/feasibility_store.py <STORE_IP> small
python3 workspace/kv_path_bench/feasibility_check.py <CLIENT_IP> <STORE_IP> small
```

客户端返回 `P0` 后在 Store 端按 Ctrl+C。最大规模时，两端把命令末尾的 `small` 改成 `max`，Store 端等 `S1`、客户端等 `P1`，然后再次 Ctrl+C。小规模是一页连续地址，最大规模是完整 128K 分散地址；每组只验证两条远端路径各一次，不采性能样本。最大规模在 Store 端申请 10 GiB segment、设置 `fabric_memory.max_capacity=16`，客户端保留约 8.6 GiB L1、每批接收 8 页。

失败只需回报终端上最后出现的短码：`F1` Store 启动或准备数据失败；`F2` 客户端 L1 分配或 Store 初始化失败；`F3` Host 暂存分配/注册失败；`F4` NPU 注册失败；`F5` Host 中转读取/校验失败；`F6` NPU 暂存读取/校验失败；`F9` 其他错误。详细输出自动写入 `/tmp/a3-kv-feasibility-{store,client}-{small,max}.log`；不用手工查日志、抄日志或输入 `tail` 命令。失败后 Ctrl+C 结束 Store，不继续扩大规模。

成功短码只证明数据路径正确，UB 实际传输仍需另查日志或计数器。这里的分散模式是确定性的非连续 page 索引映射；正式性能脚本下一次再加分批、碎片地址和性能采样。
