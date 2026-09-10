# Mooncake TCP 双机测试

独立于 `workspace/setup` 的 Fabric 脚本，不加载模型、不安装依赖、不修改系统参数。
需要两台已安装 Mooncake 的容器；target 还需要 `mooncake_master`。
普通测试不导入 torch、不设置 NPU；pinned 测试需要现有 torch/torch_npu 环境。
实际 NPU wheel 的 TCP 支持以本测试结果为准。

## 第一步：普通 TCP Store

在第二台容器、仓库根目录运行：

```bash
python3 workspace/tcp_test/check_pair.py target --local-ip <Store节点IP>
```

等到 `TCP:READY`，保持终端运行。在第一台容器执行：

```bash
python3 workspace/tcp_test/check_pair.py client \
  --local-ip <客户端IP> --target-ip <Store节点IP>
```

成功标志：`TCP:REMOTE_OK`。两端使用不同物理机器的可互通 IPv4。
默认 master 端口为 50081（两端可用 `--port` 同时修改）；TCP transport
还会建立额外连接，不能只开放 master 端口。

只有 target 贡献 256 MiB Store；各进程本地缓冲为 16 MiB。
客户端贡献为零，分别写入 4 KiB、64 KiB、1 MiB 数据，写入进程正常退出后，
再启动全新读取进程逐字节校验。每次运行使用唯一 key。
测试假定使用本脚本启动的独立 master，没有其他 Store 加入。

## 第二步：pinned 内存注册和传输

普通测试通过后，保持同一个 target 运行。在第一台运行：

```bash
python3 workspace/tcp_test/check_pair.py client \
  --local-ip <客户端IP> --target-ip <Store节点IP> --pinned
```

成功标志：`TCP:PINNED_REMOTE_OK`。写入和读取进程各自设置逻辑 NPU 0，
分配 1 GiB CPU pinned tensor，调用 `register_buffer`，然后通过
`batch_put_from` / `batch_get_into` 搬运并校验上述三个大小的数据。
这覆盖外部 pinned 内存注册和批量指针接口，但不代表整个 1 GiB 都经过传输。
可用 `--device` 修改逻辑 NPU；`--pinned-mib` 修改分配大小（最小 1 MiB）。
建议在模型停止时做该测试，减少资源干扰。

## 日志与结束

日志目录会打印为 `/tmp/mooncake-tcp-pair-*`。其中 `master.log` 是 master 日志，
`serve/probe.log`、`write/probe.log`、`read/probe.log` 是各进程完整输出；
`controller-error.log` 记录控制脚本错误。终端显示当前阶段和失败标志，
例如 `TCP:write_PIN_REGISTER_EXIT1`。失败时优先回报标志及对应 probe.log 末尾。
每个客户端进程和 target 初始化默认超时 180 秒，可用 `--timeout` 调整。

客户端结束后，在 target 终端按 Ctrl+C。脚本仅清理自己启动的进程，
临时 Store 中的数据随服务退出释放，日志保留。不修改交互式 shell；
子进程清除本测试涉及的 Fabric 配置，Store setup 明确指定 `tcp`。
同时设置 `MC_FORCE_TCP=1`，绕过 Ascend 构建在引擎初始化时自动安装
Ascend transport 的逻辑。若 wheel 未编译 TCP 或不支持该开关，仍需根据日志判断。

查看当前容器最新一次测试日志（开头和末尾）：

```bash
bash workspace/tcp_test/log.sh
```

两项测试通过后才能继续小容量 HiCache 集成。这些结果不证明 L3 缓存命中、
RDMA/Fabric 可用或正式实验性能；不应直接使用旧 Fabric 集成入口启动 TCP HiCache。

本地离线检查（不需要 Mooncake 或 NPU）：

```bash
python3 -m unittest discover -s workspace/tcp_test -p 'test_*.py'
```
