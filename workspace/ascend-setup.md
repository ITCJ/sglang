# A3 环境部署

更新：2026-09-14。本文件只记录 A3 容器的创建、依赖安装和基础诊断。
测试结果见 [fabric-note.md](fabric-note.md)，当前研究状态见
[research-status.md](research-status.md)。远端结果由操作者手工回报；本地工作区不是目标机器。

## 环境要求

目标环境为两台 Ascend 910C（A3）机器，容器使用 Ubuntu 22.04 arm64。
驱动、固件、CANN 和超节点网络由平台提供。

| 项目 | 部署记录 |
| --- | --- |
| 容器 | Ubuntu 22.04，aarch64（Ubuntu 包架构名为 arm64） |
| 镜像 | Ascend SGLang 镜像，版本系列 v0.5.16 / CANN 9.0.0 / A3 |
| 宿主机驱动 | 当前记录为 26.1.1；容器内驱动文件来自宿主机挂载 |
| Mooncake | `mooncake-transfer-engine-npu==0.3.12.post1` |
| 镜像身份 | 实际 digest 需要在正式实验前记录，不能只凭标签判断两台相同 |

## 创建容器

平台负责 Ascend 驱动和固件。不要在容器内升级宿主机驱动。

```bash
npu-smi info
grep '^Version=' /usr/local/Ascend/driver/version.info
```

在仓库目录使用本地已有的基础镜像创建新容器：

```bash
CONTAINER_NAME="<容器名>" IMAGE="<本地镜像名或ID>" \
bash workspace/setup/docker_run_sglang_ascend.sh "<代码父目录>" "<模型目录>"
```

第一个参数是包含 `sglang` 目录的父目录，第二个参数必须是存在的目录。
只测试 Store 时可以把两个参数设为同一个代码父目录。脚本使用 host 网络、
privileged、无限 memlock，并挂载 NPU 设备、Ascend driver、代码和模型；存在
`/etc/hccn.conf` 时也会挂载。它把容器中的 SGLang Python 包链接到挂载仓库，
这是开发环境，不是未修改的发行镜像。

## 安装容器依赖

进入容器后，以 root 在仓库目录运行：

```bash
bash workspace/setup/install_rdma_runtime.sh
bash workspace/setup/install_mooncake.sh
bash workspace/setup/diagnose_mooncake.sh
```

RDMA 脚本安装 `libibverbs1`、`ibverbs-providers`、`librdmacm1`、`rdma-core`、
`ibverbs-utils`、`libnl-3-200` 和 `libnl-route-3-200`，其余依赖由 apt 处理。
当前方案在容器内安装用户态包，不挂载宿主机的单个 RDMA 动态库。

安装脚本会显示阶段和错误，完整日志保存在 `/tmp/mooncake-rdma-install-*.log`。
Mooncake 检查成功时应出现 `Mooncake Transfer Engine + Store: OK` 和
`mooncake_master:` 路径。重建容器不会继承旧容器后来安装的包。

## 基础诊断

网络检查是可选的：

```bash
bash workspace/setup/check_network.sh
```

Fabric 和 RDMA 诊断脚本只用于现场检查，结果写入
[fabric-note.md](fabric-note.md)：

```bash
python3 workspace/setup/check_fabric_local.py
python3 workspace/setup/check_fabric_pair.py --help
bash workspace/setup/show_fabric_log.sh
```

脚本不会把真实容器名、IP、内部路径或凭据写入仓库。正式运行时通过参数提供，
并记录实际使用的仓库 commit、镜像 digest、wheel、系统包和启动参数。

## 参考

- [SGLang Ascend 安装](https://github.com/sgl-project/sglang/blob/main/docs/docs/hardware-platforms/ascend-npus/getting-started/installation.mdx)
- [Mooncake 安装与 Store smoke test](https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/getting_started/quick-start.md)
- [Mooncake Ascend Direct / Fabric Memory](https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/design/transfer-engine/ascend_direct_transport.md)
