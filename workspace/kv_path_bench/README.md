# A3 MLA KV 整请求传输测试

本性能套件不设置固定页数的拆批上限。每次提交完整请求，预热和计时也以完整请求为单位。JSON 标记 `measurement_protocol=whole_request_v2`，不可与旧的逐批累加样本混用。

## 路径与计时

| 路径 | 一次样本的计时范围 |
| --- | --- |
| `L2-L1_SGLKernel` | L2 已准备好，一次提交完整请求到 L1，等待 NPU 完成 |
| `L3-L2-L1_Mooncake` | 一次提交全部 page keys，完整读取到最终 L2，再一次提交 L2→L1 并同步 |
| `L3-L2_Mooncake` | 一次提交全部 page keys，完整读取到最终 L2，保留统一末尾同步 |

不再采集 staging 路径。仍是一页一个 Store key，没有聚合对象；接口内部如何调度由库决定，脚本不设置固定页数切分，也不在接口失败时悄悄拆小重试。中转先完成整请求读取再加载 L1，不增加流水。

数据为 61 层 BF16 MLA，128 tokens/page，512 维压缩 KV + 64 维 RoPE（v_buffer 是 RoPE）。L2 为 KV/RoPE 分离的 page-first，L1 为 layer-first；每页 8,994,816 bytes。保留连续/确定性分散 L1 映射，L3→L2 单段的 L2 地址组织不随该标签变化。

每路径完整请求预热 2 次、采样 10 次。每次样本是实际开始到完成的 wall time，不累加独立批次采样。准备、清零、地址列表构造和逐页校验均在计时外；每次执行后校验。CSV median_ms/p95_ms 为 ms，JSON samples_s 为秒，effective_gbps 实际单位为十进制 GB/s。10 样本 nearest-rank P95 即最大值。

## 内存

最终 ADXL L2 和 NPU L1 都容纳完整请求（各含一个保留页），没有额外 Host staging。128K 时各约 8.59 GiB。客户端 Store 内部接收池按请求向上取整至 GiB 并预留空间（128K 为 9 GiB），通过 BufferPool 借用完整 L2；不继续使用旧的固定小接收池。客户端默认按池大小设置 fabric_memory.max_capacity；已有环境变量不足时明确失败，不覆盖用户设置。性能未在新口径下远端验证，尤其需要验证大池与完整请求提交支持。

## 一次运行

两端同一提交，设备空闲、自己的模型及其他性能实验停止。先更新两端：

```sh
git pull --ff-only
git log -1 --oneline
```

Store 端准备完整数据并保持运行：

```sh
python3 workspace/kv_path_bench/feasibility_store.py <STORE_IP> max --direct-l2
```

等 `S1`，客户端运行：

```sh
python3 workspace/kv_path_bench/performance_suite.py <CLIENT_IP> <STORE_IP>
```

自动先 128-token 冒烟（0 次预热、1 次采样），再测 1K/4K/16K/64K/128K × 两种布局 × 三路径，共 30 条正式结果；失败立即停止。每档独立进程，默认超时 1800 秒，可 `--timeout` 调整。成功为 `ALL_OK`。结束后 Store 端 Ctrl+C，客户端结束释放资源；客户端 Ctrl+C 只终止其自行启动的进程组。

结果位于 `results/YYMMDD_HHMMSS/`：summary.json/csv、cli.log、各档 JSON 和日志。失败回报 `X编号 F码` 与对应日志末尾；例如第一组：

```sh
tail -n 35 workspace/kv_path_bench/results/<RUN_ID>/128-contiguous.log
```

单独一档：

```sh
python3 workspace/kv_path_bench/kv_transfer_bench.py <CLIENT_IP> <STORE_IP> --tokens 1024 --layout contiguous
```

端口集中在 bench_ports.py，master RPC 19271、管理/metrics 19273。依赖 torch/torch_npu/mooncake.store.BufferPool/sgl_kernel_npu。ADXL Host 分配仍不同于原生 HiCache pinned Host；不能把结果宣称为原生 server 性能或已确认物理 UB 带宽。

## 历史结果

`260915_110547` 的 40 条正式性能结果及其比较结论已撤回，保留文件仅供追溯，等待整请求重测。旧一页/可行性检查不是性能基线。

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

可行性脚本用 `mooncake.store.BufferPool` 从 `setup()` 已注册的 1 GiB ADXL Host 缓冲区借用接收暂存区（可行性检查的有限接收缓冲），不再额外分配并注册 Host 内存。测试结束先归还缓冲区再关闭 Store；该接口已核对官方 `v0.3.12.post1` 源码。

claim 设备、停止模型后，先在 Store 端运行（出现 `S0` 后保持运行），再在客户端运行：

```bash
python3 workspace/kv_path_bench/feasibility_store.py <STORE_IP> small
python3 workspace/kv_path_bench/feasibility_check.py <CLIENT_IP> <STORE_IP> small
```

客户端返回 `P0` 后在 Store 端按 Ctrl+C。最大规模时，两端把命令末尾的 `small` 改成 `max`，Store 端等 `S1`、客户端等 `P1`，然后再次 Ctrl+C。小规模是一页连续地址，最大规模是完整 128K 分散地址；每组只验证两条远端路径各一次，不采性能样本。最大规模在 Store 端申请 10 GiB segment、设置 `fabric_memory.max_capacity=16`，客户端保留约 8.6 GiB L1、使用有限接收缓冲循环校验。

失败只需回报终端上最后出现的短码：`F1` Store 启动或准备数据失败；`F2` 客户端 L1 分配或 Store 初始化失败；`F3` Host 暂存分配/注册失败；`F4` NPU 注册失败；`F5` Host 中转读取/校验失败；`F6` NPU 暂存读取/校验失败；`F9` 其他错误。详细输出自动写入 `/tmp/a3-kv-feasibility-{store,client}-{small,max}.log`；不用手工查日志、抄日志或输入 `tail` 命令。失败后 Ctrl+C 结束 Store，不继续扩大规模。

成功短码只证明数据路径正确，UB 实际传输仍需另查日志或计数器。这里和性能脚本的分散模式均为确定性的非连续 page 索引映射，不代表分配器长期运行后的真实碎片状态。


## 本地检查

```sh
python3 -m unittest discover -s workspace/kv_path_bench -p 'test_*.py'
```
