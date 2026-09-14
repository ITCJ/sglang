# A3 MLA KV 传输路径测试

本目录的可行性和性能脚本统一使用非默认端口：master RPC 为 `19271`，管理/metrics 为 `19273`，集中定义在 `bench_ports.py`。两端更新代码后，原启动命令不变。非默认端口仍可能被占用；若冲突，修改该文件并同步两端，不停止其他任务的服务。这两个配置只控制 master 的监听端口。

独立于模型和 HiCache，比较同一份 BF16 MLA KV 写入同一组 NPU L1 page 的耗时：

| 输出中的路径 | 计时范围 |
| --- | --- |
| A：`L2->L1` | 数据已在最终 ADXL Host L2，经 Ascend 搬运 kernel 写入 L1 |
| B：`L3->Host staging->L2->L1` | 整页读入额外 ADXL Host 暂存，拷贝 KV/RoPE 到最终 L2，再搬到 L1 |
| C：`L3->L2->L1` | 多目标读取直接写入最终 L2 的 KV/RoPE，再搬到 L1 |

性能脚本固定只测 A/B/C，L3 直接到 NPU 的路径已禁用，不申请或注册 NPU 接收暂存区。原 `--skip-direct` 参数仅为兼容旧命令保留，不改变行为。

数据按 DeepSeek V3.1 的 61 层、每页 128 token、BF16、每 token 512 维压缩 KV + 64 维 RoPE 构造。一页一个 Store 对象（8994816 bytes），三种方式统一使用“整页 KV 后接整页 RoPE”的排列和 `-split` key 前缀。Host L2 和 NPU L1 各自将 512/64 分别存入两个缓冲区；这里的 `v_buffer` 是 RoPE，不是传统注意力里单独的 V。

远端不能复制命令：先在两端仓库各手输一次 `git pull --ff-only`。claim 设备之前，分别手输一条不初始化 NPU 的命令；脚本会打印 commit，需确认两端与交接的 commit 相同：

```bash
python3 workspace/kv_path_bench/preclaim_check.py store
python3 workspace/kv_path_bench/preclaim_check.py client
```

两行分别在 Store 端、客户端运行，不是在一台机器上连续运行。看到 `PRECLAIM_OK` 才继续。此检查不连接两端、不验证 Fabric，也不替代 claim 后的小规模传输测试。仓库不保存真实 IP、容器名和凭据。

## 三种路径性能测试

两端使用同一环境和提交，设备已可用、模型已停止。先测 small。Store 端运行（旧的非 direct-l2 Store 需先 Ctrl+C 停止）：

```bash
python3 workspace/kv_path_bench/feasibility_store.py <STORE_IP> small --direct-l2
```

看到 S0 后，在客户端运行：

```bash
python3 workspace/kv_path_bench/kv_transfer_bench.py <CLIENT_IP> <STORE_IP> small
```

成功只打印一行，例如 `T128 A=1.000 B=2.000 C=1.500`：T 后是 token 数，A/B/C 是三条路径的累加批次耗时中位数，单位毫秒；这仅为格式示例，不是实测数据。回报这一行即可。详细结果写入 `/tmp/a3-kv-perf-128.json`，底层日志写入 `/tmp/a3-kv-perf-128.log`。失败输出 F2（L1/Store 初始化）、F3（Host 分配）、F5（路径执行/校验）、F9（其他），日志中记录路径和批次。

small 成功后结束 Store，两端把 `small` 改成 `max`，等 Store S1 后再运行客户端；结果为 `T131072 ...`，文件名中的 128 改为 131072。若对应规模的 direct-l2 Store 已在运行，无需重启。每轮结束后 Store Ctrl+C；失败不扩大规模。

比较口径：

- 三种方式使用同一块最终 ADXL L2、相同对象、最多 8 页的批次和同一组非连续 L1 目标地址。B 也改为相同对象顺序及 ADXL L2，以隔离额外暂存拷贝的影响；它不再沿用旧可行性脚本的交错对象和 pinned L2。
- 每个批次、每条路径预热 2 次，记录 10 次（可用 `--warmup` / `--repeats` 修改），批次间轮换 A/B/C 顺序。A 的本地数据准备、各路径清零和地址列表构造在计时外；接收、B 的额外 CPU 拷贝、L2→L1 调用和批次末尾 NPU 同步在计时内。每条路径每批最后一次执行后都逐页校验 L1。
- 第 r 个总样本是所有批次第 r 次计时之和。JSON 保存批次原始样本、累加样本、中位数、P95、逻辑 KV bytes / 中位数得到的有效 GB/s、L1/L2/额外暂存大小。这是逐批预热的串行搬运比较，不是连续请求延迟、模型吞吐或物理链路带宽；没有流水重叠。
- max 保留完整 128K L1（约 8.6 GiB），L2 最多 9 页（含保留页），额外 Host 暂存最多 8 页，两者从同一个 1 GiB Store 内部缓冲池借用。所有路径测试期间两块 Host 内存都保留，分配和释放不计时。

两端需有 `torch`、`torch_npu`、`mooncake.store.BufferPool`，客户端还需 `sgl_kernel_npu`。旧 `--local-ip`、`--master-ip`、`--tokens` 入口仍可用；自选容量时 Store 也需准备相同容量的 split 对象。结果不单独证明实际使用了 UB 物理链路。

本地无需 NPU 的数据布局检查：

```bash
python3 -m unittest discover -s workspace/kv_path_bench -p 'test_*.py'
```

## 可行性验证（不计时）

### 直接写入最终 L2

先停止旧 Store，两端拉取同一提交；先只测 small。Store 端运行：

```bash
python3 workspace/kv_path_bench/feasibility_store.py <STORE_IP> small --direct-l2
```

等 S0 后，客户端运行：

```bash
python3 workspace/kv_path_bench/feasibility_check.py <CLIENT_IP> <STORE_IP> small --direct-l2
```

客户端 D0 表示一页 L3 直接写入最终 ADXL Host L2、L2 内容校验、同一内存到 L1 搬运与校验全部通过。D1 是对应 max 结果，先等 small 通过再扩大。F3 为 Host 分配问题，F5 日志中的阶段区分 direct L2 read 与 ADXL Host to NPU；失败只回报短码，详细日志沿用原位置。结束后 Store 按 Ctrl+C。

此模式仍是一页一个 key，使用独立的 `-split` key 前缀；对象内容调整为整页压缩 KV 后接整页 RoPE，调用 `batch_get_into_multi_buffers` 直接写入两处最终 L2 地址。L2 从已注册 BufferPool 借用，保持租约直到搬运结束，不再申请另一块 pinned L2，不发生接收暂存到 L2 的拆分拷贝。清零和 CPU/NPU 数据校验仅用于此可行性验证，不采性能。若后续做路径性能对比，必须统一各路径的对象顺序；不能直接与旧交错对象格式的计时作公平比较。

### 原 Host 中转及双路径模式

当前先测 Host 路径：客户端原命令末尾加 `--host-only`，Store 命令不变。`H0` / `H1` 分别表示 small / max 的 `L3->Host->L1` 逐页校验通过；不代表 NPU 直达通过。默认双路径模式也会先输出 Host 成功码，再分配和注册 NPU 暂存区，最后两条都通过才输出 `P0` / `P1`。`F4` 表示 NPU 暂存分配或注册失败。NPU 分配调查见 [交接文档](NPU_FABRIC_HANDOFF.md)。

Python、底层库和子进程的标准输出/错误均写入日志；终端只显示短结果码。

可行性脚本用 `mooncake.store.BufferPool` 从 `setup()` 已注册的 1 GiB ADXL Host 缓冲区借用接收暂存区（最多 8 页），不再额外分配并注册 Host 内存。测试结束先归还缓冲区再关闭 Store；该接口已核对官方 `v0.3.12.post1` 源码。

claim 设备、停止模型后，先在 Store 端运行（出现 `S0` 后保持运行），再在客户端运行：

```bash
python3 workspace/kv_path_bench/feasibility_store.py <STORE_IP> small
python3 workspace/kv_path_bench/feasibility_check.py <CLIENT_IP> <STORE_IP> small
```

客户端返回 `P0` 后在 Store 端按 Ctrl+C。最大规模时，两端把命令末尾的 `small` 改成 `max`，Store 端等 `S1`、客户端等 `P1`，然后再次 Ctrl+C。小规模是一页连续地址，最大规模是完整 128K 分散地址；每组只验证两条远端路径各一次，不采性能样本。最大规模在 Store 端申请 10 GiB segment、设置 `fabric_memory.max_capacity=16`，客户端保留约 8.6 GiB L1、每批接收 8 页。

失败只需回报终端上最后出现的短码：`F1` Store 启动或准备数据失败；`F2` 客户端 L1 分配或 Store 初始化失败；`F3` Host 暂存分配/注册失败；`F4` NPU 注册失败；`F5` Host 中转读取/校验失败；`F6` NPU 暂存读取/校验失败；`F9` 其他错误。详细输出自动写入 `/tmp/a3-kv-feasibility-{store,client}-{small,max}.log`；不用手工查日志、抄日志或输入 `tail` 命令。失败后 Ctrl+C 结束 Store，不继续扩大规模。

成功短码只证明数据路径正确，UB 实际传输仍需另查日志或计数器。这里和性能脚本的分散模式均为确定性的非连续 page 索引映射，不代表分配器长期运行后的真实碎片状态。
