# 环境准备与检查

本目录存放容器启动、依赖安装、网络检查、Mooncake/Fabric 验证及模型服务检查脚本。部署经验见 [ascend-setup.md](../ascend-setup.md)。原来 `workspace/<脚本>` 的命令现在使用 `workspace/setup/<脚本>`。

文档分工：环境部署看 [ascend-setup.md](../ascend-setup.md)，测试事实和结果看
[fabric-note.md](../fabric-note.md)，当前研究状态看 [research-status.md](../research-status.md)，
当前 KV 路径任务看 [kv-path-plan.md](../kv-path-plan.md)。

```bash
bash workspace/setup/check_model.sh --suite
```

模型启动脚本默认 TP16+DP1。`MOONCAKE_CONFIG` 非空时会开启 HiCache，仅供实验目录的集成入口调用。

MemFabric Host→NPU 直达实验：两端现有 A3 容器内运行，直接替换为固定版本 1.1.5，并检查 BM 接口，不初始化 NPU。安装前结束自己的实验进程。

```bash
bash workspace/setup/install_memfabric.sh
```

`MF0` 表示安装及接口检查通过，`MF1` 表示失败；完整日志在 `/tmp/a3-memfabric-install.log`。这不代表直达传输已验证。

离线测试：

```bash
python3 -m unittest discover -s workspace/setup -p test_chat_client.py
```
