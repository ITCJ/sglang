# 一页 Host → NPU 直达验证

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
