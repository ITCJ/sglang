# 环境准备与检查

本目录存放容器启动、依赖安装、网络检查、Mooncake/Fabric 验证及模型服务检查脚本。部署经验见 [ascend-setup.md](../ascend-setup.md)。原来 `workspace/<脚本>` 的命令现在使用 `workspace/setup/<脚本>`。

```bash
bash workspace/setup/check_model.sh --suite
```

模型启动脚本默认 TP16+DP1，当前聊天测试已由操作者回报 `M1:OK`。`MOONCAKE_CONFIG` 非空时会开启 HiCache，仅供实验目录的集成入口调用。

离线测试：

```bash
python3 -m unittest discover -s workspace/setup -p test_chat_client.py
```
