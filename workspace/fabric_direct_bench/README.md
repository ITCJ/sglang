# Host → NPU 直达验证与性能实验

## 自动性能实验

在已通过一页验证的同一环境运行。两端使用相同模式，先源端，看到 `FR` 再启动客户端：

```sh
python3 workspace/fabric_direct_bench/check.py source <SOURCE_IP> --performance
python3 workspace/fabric_direct_bench/check.py client <CLIENT_IP> <SOURCE_IP> --performance
```

一次完成一页冒烟，以及 1K、4K、16K、64K、128K × 连续/分散布局，共 10 组正式测量。路径编码 D。默认每批最多 8 页、预热 2 次、测量 10 次，与现有 A/B/C 一样将各批对应采样相加，再取中位数/P95；不是连续端到端请求耗时。计时包括 GH2L 批量调用、BM wait 和 NPU 同步；分配、地址列表构造、源准备、清零、逐页校验不计时。每页仍是独立对象地址，源按页连续分配属于本实验的分配选择，不声称模拟 Store 分配碎片。

两端各贡献 10 GiB BM Host 池，客户端的 Host 池不接收 KV；源一次准备完整 128K 数据。客户端最大 L1 约 8.59 GiB；初始化/库内部额外资源不含在这些容量中。不要与自己的 A/B/C 实验同时运行。

两端各自在 `workspace/fabric_direct_bench/results/YYMMDD_HHMMSS/` 保存 `cli.log`、`native.log`、`status.json`；客户端另有 `summary.json`、`summary.csv`、各组 JSON。时间戳为本机时间，组间实时保存；最终成功仍是客户端 `FP`、源端 `FD`。终端 D 值和 CSV 耗时单位 ms，JSON `_s` 字段为秒，带宽为 GB/s。结果不提交 Git。

Ctrl+C 终止自己的工作进程组；性能模式总超时默认 3600 秒（是防挂死上限，不是预计耗时），可用 `--timeout` 调整。失败回报短码；诊断命令仍为 `python3 workspace/fabric_direct_bench/check.py client --diagnose`。未完成的组不会冒充成功，已完成的组保留。绕过 Store 的 D 不含 Store 管理开销，需与 A/B/C 分开标识。

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
