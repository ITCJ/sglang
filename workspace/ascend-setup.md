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

## UNIDEX 三路径：一键创建并初始化容器

在两台 A3 **宿主机**的本仓库中各运行一次：

```bash
bash workspace/setup/start_unidex_container.sh <LOCAL_IMAGE_NAME_OR_ID>
```

可使用机器上已有的 SGLang v0.5.20 镜像，直接传它的本地名称或 ID；无需 kernel 源码目录、模型目录或手填镜像 digest。脚本自动定位当前仓库，创建独立命名的容器，使用 host 网络、privileged、32 GiB `/dev/shm`、无限 memlock，并挂载仓库、宿主机驱动及存在的 Ascend/RDMA 设备配置。已有容器不变；可用第二个参数 `<NEW_CONTAINER_NAME>` 指定新名字，重名即停止。

SGLang v0.5.20 与 sgl-kernel-npu 2026.9.0 是独立版本：前者是基础镜像所标注的 SGLang 版本，后者是 NPU 算子包版本。本实验从当前挂载仓库调用独立脚本，直接导入镜像里已安装的算子，不升级 SGLang 仓库、torch、CANN、驱动、DeepEP 或 custom_ops，也不链接替换镜像的 SGLang Python 包。

初始化先检查镜像中的 UNIDEX wrapper 和 native ops；已具备所需接口则保留。缺失或无法加载时，仅在 Python 3.11 / torch、torch_npu 2.10.0 / CANN 9.0.0 / aarch64 组合核验通过后，下载固定的官方 `2026.9.0` A3 zip，校验 SHA256 `68372ac96c328fabdc0b7c9c0cc19c6540fbe775ad35ab7ab83e9446029dcfc4`，只提取并以 `--no-deps` 安装 `sgl_kernel_npu` wheel。其他组合明确失败，不强装这个 wheel。MemFabric 的必要接口已具备就保留，否则安装固定 `memfabric-hybrid==1.1.5`。需要补包时，容器须能访问官方 GitHub release 与配置的 Python 包源；下载失败就停止，没有未固定版本回退或源码编译。

成功输出 `UNIDEX_ENV_READY`，交互式启动会自动进入容器；非交互启动会打印 `docker exec -it <CONTAINER_NAME> bash -l`。CANN 环境和 A3 SOC 设置由登录 shell 加载，后续进入也使用 `bash -l`。自动保存基础镜像 ID/RepoDigests、仓库 commit、实际 docker 参数、安装前后版本、下载包 SHA256 和安装日志到打印的 `LOG_DIR`（本地忽略的 `workspace/unidex_copy_bench/logs/` 下）。`UNIDEX_ENV_READY` 只代表安装和导入/API 检查通过；不会运行模型、NPU 数据正确性校验或性能实验。

首次停止目标模型后，按 [统一实验入口](unidex_copy_bench/README.md#统一交接) 在两端容器里运行 `--check-only`，三条路径小规模校验通过后再测性能。两端先 `git pull --ff-only`、`git log -1 --oneline` 核对交付 commit。

初始化失败执行：

```bash
tail -n 80 <LOG_DIR>/setup.log
```

回传末尾 80 行、退出码、`setup-status.json` 和存在时的 `initialize-status.json`。失败时保留新容器用于查看依赖加载错误，不继续跑实验。测试结束退出 shell 后，容器仍保留，按脚本打印的名字执行 `docker stop <CONTAINER_NAME>`；确认日志已回传且无需保留环境后再自行 `docker rm <CONTAINER_NAME>`。不删除镜像或任何原有容器。
