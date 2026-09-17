# MemFabric 整请求传输实验

> 最新结果：用户于 2026-09-16 提供的截图已归档到 [results/260916_174238](results/260916_174238/EXTRACTION.md)，跨实验结论见 [最新分析](../bench_analysis/260916/analysis.md)。本次三组为当前基准，旧性能结果无效。截图未包含远端 commit、实际命令或校验开关；正文中“尚需远端验证”是交付时状态，不能用汇总截图替代数据正确性证明。

性能测试采用 `whole_request_v2`：脚本不设置固定页数拆批，每条路径一次提交完整请求所需的对象/地址列表。每个样本是真实的整请求完成时间，不再累加独立批次的测量。

| 路径 | 计时内操作 |
| --- | --- |
| `L2-L1_MemFabric` | 本地 L2 已准备，一次整请求 GH2L + wait |
| `L3-L2-L1_MemFabric` | 一次整请求 G2G + wait，再一次整请求 GH2L + wait |
| `L3-L1_MemFabric` | 远端 Host 直接到最终 L1，一次整请求 GH2L + wait |
| `L3-L2_MemFabric` | 远端 Host 到本地最终 L2，一次整请求 G2G + wait |

均包含末尾 NPU 同步。中转路径先完成全部读取再加载 L1，不增加流水。接口内部调度由 MF 决定；若完整地址列表超过接口限制，报错停止，不静默缩成小批次。

数据为 61 层、BF16、128-token page、512+64 维 MLA KV。源一页全部 KV 后接 RoPE，最终 Host L2 为分离 KV/RoPE 的 page-first，NPU L1 为 layer-first。真正离散模式在远端 Host、最终 L2 和 L1 都让有效 page 之间间隔一个完整空 page，page 内部仍连续；四条路径共享同一逻辑映射。旧 scattered 结果只是连续物理页集合上的置换，不作为本轮离散结果。

每路径按完整请求预热 2 次、采样 10 次；一页冒烟不预热只执行一次。地址构造、清零和准备在计时外。性能模式默认关闭数据校验（含一页冒烟）；客户端加 `--validate` 才会每轮在计时外逐字节校验，此时 L3→L2 单段通过额外的计时外 GH2L 读出验证。关闭校验仍检查接口返回值并等待传输完成，JSON 记录 `validation_enabled=false`、`correct=null`，不宣称内容正确。清零使用一页大小的 CPU scratch 循环写入，这不是计时内的数据传输拆批。

性能模式下两端各保留 28 GiB BM Host 池：源端同时保存 128K 连续源和一份地址范围独立的 128K 离散源，客户端可容纳约 17.16 GiB 的最大离散 L2。128K NPU L1 连续布局约 8.59 GiB、离散布局约 17.16 GiB。没有额外完整请求大小的 CPU 零缓冲；该容量需求必须先在远端确认可分配。

## 聚合执行

设备空闲，自己的模型及其他性能测试停止，两端同一提交且 memfabric_hybrid/BM 可用。先在两端更新并核对：

```sh
git pull --ff-only
git log -1 --oneline
```

需要在性能套件开头增加一次 4K scatter 传输与逐字节校验时，源端运行：

```sh
python3 workspace/fabric_direct_bench/check.py source <SOURCE_IP> --performance --preflight-validate
```

等 `FR` 后，客户端：

```sh
python3 workspace/fabric_direct_bench/check.py client <CLIENT_IP> <SOURCE_IP> --performance --preflight-validate
```

同一入口会在校验通过后自动继续完整性能矩阵，性能部分不校验；校验失败则停止。若不需要开头校验，源端直接运行：

```sh
python3 workspace/fabric_direct_bench/check.py source <SOURCE_IP> --performance
```

客户端：

```sh
python3 workspace/fabric_direct_bench/check.py client <CLIENT_IP> <SOURCE_IP> --performance
```

性能套件自动跑一页冒烟和 1K/4K/16K/64K/128K × 连续/离散 × 四路径，共 40 条正式结果。`--validate` 保留为整套逐轮校验的诊断选项，不能与 `--preflight-validate` 同时使用。失败即停止，成功为客户端 `FP`、源端 `FD`。

失败在相应端运行：

```sh
python3 workspace/fabric_direct_bench/check.py client --diagnose
```

源端把 client 换成 source。回报短码和诊断信息。结果位于 `results/YYMMDD_HHMMSS/`，包含 cli/native 日志、status.json、客户端汇总与每条路径 JSON。CSV 耗时为 ms，JSON samples_s 为秒，带宽为十进制 GB/s。10 样本 P95 为最大值。

`260915_104705` 的全部 40 条正式性能结果及比较结论已撤回，文件仅作历史记录。旧结果不能混入整请求性能分析。`FP` / `FD` 在性能默认模式只表示运行完成，启用 `--validate` 后才包含数据校验通过。独立的一页正确性检查（不加 `--performance`）仍始终校验。新结果只说明具体传输实现下的路径成本，不代表 server TTFT 或物理链路上限。

## 一页验证

**基于官方代码的候选实现，尚未在 A3 验证。** 这里只验证正确性，不测性能，也不代表 Mooncake Store 直读 NPU 已解决。

路径：远端 BM Host DRAM → MemFabric BM `SDMA / GH2L` → 本地最终 NPU L1。源不是 NPU HBM；不分配接收 KV 暂存区，不执行接收后的布局转换。通过不能单独证明实际物理链路是 UB，需要另行确认。

复用 `../kv_path_bench` 的数据格式、校验和日志函数，不运行 SGLang 或 Mooncake。与现有性能脚本相同：一页 128 tokens、61 层、BF16 压缩 KV 512 + RoPE 64，共 8,994,816 bytes；源对象先全部 KV 后全部 RoPE。122 个片段直接写入 `[layer, page, token, 1, dim]` 的第 1 页，第 0 页保留并检查未被覆盖。

遵循官方 BM DRAM 示例，两端各贡献 1 GiB Host 内存，BM HBM 池为 0。客户端贡献的 Host 池不接收 KV，只为沿用示例初始化方式。客户端另分配约 18 MiB 最终 NPU L1。初始化和校验可能有库内部资源开销，不能把“脚本无暂存”解释为库内部绝无拷贝。

## 运行

在两台 A3 的现有 CANN / torch_npu 容器内运行，需要安装包含 BM API 的 `memfabric_hybrid`。脚本不自动安装依赖。两端各需要一个可用 NPU，默认 device 0，可追加 `--device N`。不要与自己的性能测试同时运行。无需启动 Mooncake Store。

两端先运行 `bash workspace/setup/install_memfabric.sh`，固定安装 1.1.5 并检查必要接口，成功输出 `MF0`。此版本仍需远端传输验证。

两端在仓库根目录更新并核对提交：

```sh
git pull --ff-only
git log -1 --oneline
```

源端：

```sh
python3 workspace/fabric_direct_bench/check.py source <SOURCE_IP>
```

看到 `FR` 后，在另一台机器运行：

```sh
python3 workspace/fabric_direct_bench/check.py client <CLIENT_IP> <SOURCE_IP>
```

使用双方可达、符合测试网络的本机 IP。端口为 19571（BM 控制服务）、19573（脚本控制）、19575（BM NIC 配置），避免默认端口；TCP 控制连接只交换状态，KV 通过 BM 传输。

- `FR`：源端等待客户端；此时还没完成 BM 初始化。
- `FS`：源 Host 数据准备完成。
- **客户端 `FP`、源端 `FD`：完整传输、逐字节校验及清理通过。**
- `F1`：依赖/API；`F2`：控制连接；`F3`：BM 初始化/内存池；`F4`：源数据准备；`F5`：直写 NPU；`F6`：校验；`F9`：清理/进程异常；`FT`：超时。

屏幕只显示短码，详细日志在 `/tmp/a3-fabric-source.log` 和 `/tmp/a3-fabric-client.log`。失败时回报短码，需要详情再执行下面一条命令，只输出一行（源端把 `client` 换成 `source`）：

```sh
python3 workspace/fabric_direct_bench/check.py client --diagnose
```

正常结束自动清理；随时 Ctrl+C，显示 `STOP`，仅终止脚本自己的工作进程组。默认总超时 180 秒，包含源端等待手工启动客户端的时间；不会无限等待。结果 JSON 与日志同目录、同名前缀，每次覆盖。

## 依据与边界

- [官方 Python API](https://github.com/Ascend/memfabric_hybrid/blob/master/doc/pythonAPI.md)：BM `create2`、`GH2L` 和批量拷贝。
- [官方绑定实现](https://github.com/Ascend/memfabric_hybrid/blob/master/src/smem/csrc/python_wrapper/memfabric_hybrid/pymf_hybrid.cpp)：Host/Device 内存类型及接口签名。
- [官方多节点 DRAM 示例](https://github.com/Ascend/memfabric_hybrid/blob/master/examples/memory_pool/02_scale_out/02_multi_node_multi_device_dram/02_multi_node_multi_device_dram.py)：两端 Host 内存贡献和初始化。

参考的是上游 master，远端安装版本可能不同；运行时检查必要 API 并记录版本。先用这一页验证当前环境是否支持这组接口，不预先宣称可靠可行。绕过 Store 后不包含对象查找等管理开销；后续性能对比必须将其标为新增传输路径，不能据此直接下 HiCache/Mooncake 架构性能结论。
